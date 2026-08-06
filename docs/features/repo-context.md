# 仓库上下文（repo context）

> 代码：`moss/workspace.py` · `moss/repo_map.py` · `moss/ignore.py`
> 设计稿：[spec-01](../specs/spec-01-repo-context.md)

模型看不见你的仓库。它只能看见 moss 每轮塞进 prompt 前缀的那几百个 token。
这一层的全部工作，就是**在极小的预算里回答"这是个什么仓库、现在是什么状态、
这次任务该从哪开始看"**。

四个来源：git 事实、项目文档、repo map、工作区快照。

---

## 1. git 事实

`collect_git_facts(cwd)` 采集分支、HEAD、status 计数与最近 commit。

**两个子进程，一次采集**（status + log）。一轮循环里 prefix 可能被刷新多次，
每次都 fork 两个 git 进程是纯浪费。

**缓存命中条件是"TTL 未过期 **且** `.git` 指纹未变"，两者缺一不可**：

- 只看 `.git` 指纹会漏掉未跟踪文件的新增——新建一个文件不写 `.git/index`。
- 只看 TTL 会在 commit 之后的短窗口里返回过期状态。

TTL 是 500ms（`GIT_FACTS_TTL_S`）。risky 工具执行后**强制调用
`invalidate_git_facts_cache()`**——工具刚改完工作区，下一轮 prefix 里的 status
不能还是执行前的样子。

status 条目上限 20 条（`STATUS_ENTRY_LIMIT`）。超了只报计数，不逐条列。

---

## 2. 项目文档：分层发现 + 就近加载

### 分层发现

`discover_docs(repo_root, cwd)` 从仓库根一路走到当前工作目录，
按名单收集项目文档（`README.md` / `AGENTS.md` / `CLAUDE.md` 等，见 `DOC_NAMES`）。
名单可以被 `.moss/config.json` 的 `repo_context.doc_names` **整体覆盖**。

同一个指令在不同层级给出冲突要求时记 `instruction_conflict` 事件——
不静默取其一，因为"哪份赢了"直接决定 agent 的行为。

### 就近加载

`find_nearest_instruction_docs` 只找 `AGENTS.md` / `CLAUDE.md`。
它**不在 prefix 里一次性全塞进去**，而是在模型第一次碰到某个子目录时
（由 `tool_executor` 触发）才注入那个目录的就近文档，记 `instruction_loaded`。

原因很直白：一个 monorepo 里可能有几十份 `AGENTS.md`。
一次性全注入既装不下，也让模型分不清哪份跟当前的工作有关。

### 预览与指纹的分歧（重要）

进 prompt 的是**裁剪过的预览**（`DOC_PREVIEW_BUDGET = 1200` 字符，
取前 12 行 + 摘要）。但 `WorkspaceContext.fingerprint()` 用的是**文档全文** digest。

这两者必须不同。用预览算 hash 的话，README 第 1200 字符之后的改动
不会让 prompt cache 失效——模型看到的是旧内容，缓存却认为没变。
指纹还带 `WORKSPACE_FINGERPRINT_VERSION = "ws-v2"` 前缀，
指纹算法本身变了也能整体失效。

---

## 3. repo map

`repo_map.py` 生成"目录骨架 + 符号索引"，缓存在 `.moss/cache/repo_map.json`。

| 维度 | 做法 |
| --- | --- |
| 目录树 | 最大深度 3，每目录最多 20 项 |
| Python 符号 | stdlib `ast`，拿到真正的 def/class 签名 |
| 其它语言 | 行首前缀匹配（`func `/`class `/`export ` 等） |
| 排序 | 入口文件优先 + 最近修改时间加权 |
| 预算 | 默认 800 token，树占 60%，其余给符号 |
| 跳过 | 二进制探测（前 1024 字节）、超过 1MB 的文件 |

**为什么不用 tree-sitter**：那是第三方依赖。
stdlib `ast` 覆盖了本项目最需要的 Python，其它语言用行首前缀——
准确率不如 AST，但对"给模型一个起点"这个用途足够了，
而且它永远不会因为装不上依赖而失效。见
[decisions/0001-zero-dependencies.md](../decisions/0001-zero-dependencies.md)。

`MOSS_REPO_MAP=off` 是一键回退：整段回到没有 repo map 之前的 prefix，
这也是消融实验的基线。

### 起点锚

`rank_relevant_files(repo_map, query, limit=5)` 用查询词（驼峰拆分 + 停用词过滤）
对文件打分，给出每轮的 `Likely relevant files`。

这不是检索系统，是**起点提示**。它错了不会让任务失败，只会浪费一两轮。
但它错得有多频繁是可以量化的：模型第一次真正命中的文件不在候选里时记 `anchor_miss`。

---

## 4. 工作区快照

用途是回答"上一个工具到底改了什么"。risky 工具**每次调用前后各扫一遍**，
所以性能敏感。

### 策略

`MOSS_SNAPSHOT_STRATEGY`：

| 策略 | 做法 |
| --- | --- |
| `git` | 只对 `git status` 给出的变更集 lstat |
| `walk` | 全量遍历工作区 |
| `auto`（默认） | 有 git 用 git，否则 walk |

### diff 用 `(mtime_ns, size)`，不做内容 hash

对每次 risky 调用都算全仓库内容 hash 是不可接受的开销。
代价是两个**已知盲区**，写在这里免得将来被当成 bug 重新发现一次：

- walk 策略下，同尺寸覆盖写 + 还原 mtime 判不出来（git 策略靠变更集兜底）。
- `chmod` 不可见。

### 遍历纪律

- 不跟随符号链接目录——防死循环。
- 忽略 `IGNORED_PATH_NAMES`（`.git` / `.moss` / 各种 cache / venv）。
- 叠加 `.gitignore` 与 `MOSS_SNAPSHOT_EXCLUDE`。

---

## 5. ignore 的边界（安全相关）

`ignore.py` 是手写的 `.gitignore` 匹配器。快照与 repo map 共用同一套忽略口径。

**安全判定绝不依赖它。** 路径是否越界由 `Moss.path()` 的 resolve + 前缀判定回答
（见 [tool-safety.md](tool-safety.md#2-路径锚定)）。
ignore 只用来**少扫、少展示**——它的作用是省 token 和省 IO，不是拦截。

把这两件事混在一起是个经典错误：一旦忽略规则被写成安全边界，
写一条 `!.env` 的否定规则就能把敏感文件重新暴露出来。

---

## 6. 进 prompt 的样子

上面四块最终拼成 prefix 里的 workspace 段：

```
## Workspace
root: /path/to/repo   cwd: /path/to/repo/sub
git: main @ a5a1a36  ·  3 modified, 1 untracked
docs: README.md, CLAUDE.md (preview…)

## Repo map
moss/
  agent_loop.py   AgentLoop.run, AgentLoop._execute_tool_batch, …
  ...

## Likely relevant files
moss/context_manager.py, moss/token_budget.py, …
```

这一整段属于 prefix，占总预算 35%（与稳定头共享）。
**它随着 agent 自己写文件而变化**，所以 prompt cache key 只覆盖
身份/规则/Tools/Skills 段，不覆盖 workspace 段——
否则 agent 每写一个文件缓存键就抖一次，缓存等于没开。
详见 [prompt-context.md](prompt-context.md#3-缓存键为什么不是整段-hash)。

---

## 7. 相关配置

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `MOSS_REPO_MAP` | on | repo map 总开关，off 一键回退 |
| `MOSS_REPO_MAP_BUDGET` | 800 | repo map 的 token 预算 |
| `MOSS_SNAPSHOT_STRATEGY` | auto | git / walk / auto |
| `MOSS_SNAPSHOT_EXCLUDE` | 空 | 额外排除的 glob，逗号分隔 |
| `.moss/config.json` → `repo_context.doc_names` | 内置名单 | 整体覆盖文档发现名单 |

## 8. 相关 trace 事件

`repo_map_built` · `anchor_miss` · `instruction_loaded` · `instruction_conflict`
