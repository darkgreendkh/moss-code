# Spec 01 — 代码仓库上下文设计

| 项 | 值 |
| --- | --- |
| 状态 | Draft |
| 对应优化章节 | [第 1 章](../plans/archive/2026-agent-upgrade-plan.md)（1.1–1.6） |
| 优先级 | 1.3 / 1.6 是 P0；1.1 / 1.2 / 1.4 / 1.5 是 P1 |
| 依赖 | 无（1.4 的打分复用 [spec-05](spec-05-memory.md) 的 BM25 实现，可先落地简化版） |
| 被依赖 | [spec-05](spec-05-memory.md)（符号级 file summary）、[spec-06](spec-06-context.md)（`Likely relevant files` 段进预算） |

## 1. 背景与问题

模型对"这个仓库长什么样"的全部认知，就是 `git status` + 4 份被砍到 1200 字符的文档。于是每个任务的前 3–5 步都花在 `list_files` → `read_file` 认路上；而这份 workspace 文本对所有任务又是同一份，与"这次要干什么"无关。

同时有三个隐蔽的正确性问题：workspace 指纹是对**裁剪后**的文本算的（第 1200 字符之后的改动不会让缓存失效）、第二轮起 cwd 退化成 repo_root、快照用 `(mtime_ns, size)` 且不看符号链接。

## 2. 目标 / 非目标

**目标**

1. 给模型一份便宜、确定性的仓库地图（目录骨架 + 符号索引），把"认路"从工具步数变成 prompt 里的 800 token。
2. 项目文档从"固定 4 个 + 硬截断"改成"分层发现 + 结构摘要 + 可取回指针"。
3. git 采集从每轮 4 个子进程降到"合并 2 次 + 缓存"。
4. 工作区快照从 O(全仓) 降到 O(变更集)，并补上符号链接与同尺寸改写两个漏检。
5. 修掉指纹/cwd 两个身份缺陷。

**非目标**

- 不引入 tree-sitter / LSP / embedding。Python 用 stdlib `ast`，其它语言用行首正则。
- 不做 call graph / 依赖图 / 变更影响分析（真需要时另开 spec）。
- 不改 `(mtime_ns, size)` 这个快照口径（CLAUDE.md 的既定性能约定），只补它的边界条件。

## 3. 现状（代码事实）

| 事实 | 位置 |
| --- | --- |
| `DOC_NAMES` 固定 4 个，只扫 repo_root 与 cwd，每份 `clip(..., 1200)` | [moss/workspace.py:17](moss/workspace.py#L17)、[moss/workspace.py:119](moss/workspace.py#L119) |
| `build()` 每次跑 4 个 git 子进程（rev-parse / branch / symbolic-ref / status / log），各 5s timeout | [moss/workspace.py:89](moss/workspace.py#L89) |
| `status` 硬 `clip(..., 1500)` | [moss/workspace.py:128](moss/workspace.py#L128) |
| `fingerprint()` 对已裁剪的 `project_docs`/`status` 算 sha256 | [moss/workspace.py:153](moss/workspace.py#L153) |
| `refresh_prefix` 传 `self.root`（repo_root），丢掉 invocation cwd | [moss/runtime.py:236](moss/runtime.py#L236) |
| `capture_snapshot` 全量 walk，`IGNORED_PATH_NAMES` 固定，跳过 symlink 而非 lstat | [moss/workspace.py:21](moss/workspace.py#L21) |
| risky 工具前后各扫一次全仓 | [moss/tool_executor.py:228](moss/tool_executor.py#L228) |
| 现成的行首签名正则可复用 | [moss/features/memory.py:571](moss/features/memory.py#L571) |

## 4. 设计

### 4.1 新增模块与数据结构

```
moss/ignore.py       # 手写 .gitignore 匹配器（fnmatch 级，零依赖）
moss/repo_map.py     # 目录骨架 + 符号索引 + 缓存
```

```python
# moss/ignore.py
class IgnoreRules:
    """按 .gitignore 语义决定路径是否被忽略。

    为什么存在：workspace 快照、repo map、list_files 三处都需要同一套忽略口径，
    否则模型看到的目录树和快照统计的文件集会对不上。
    支持子集：# 注释、! 取反、目录尾斜杠、* / ** / ?、绝对锚定（前导 /）。
    不支持：字符类 [a-z]（罕见，命中则整行忽略并在 stderr 警告一次）。
    """
    @classmethod
    def load(cls, root: Path) -> "IgnoreRules": ...
    def match(self, rel_path: str, *, is_dir: bool) -> bool: ...
```

```python
# moss/repo_map.py
@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str          # module | class | def | async def | other
    line_start: int
    line_end: int

@dataclass(frozen=True)
class FileEntry:
    path: str          # 相对 repo_root，POSIX 分隔符
    size: int
    mtime_ns: int
    symbols: tuple[Symbol, ...]

@dataclass(frozen=True)
class RepoMap:
    entries: tuple[FileEntry, ...]
    tree_text: str          # 已渲染的目录骨架
    cache_key: str
    built_at: str
    truncated: bool         # 是否因预算折叠了内容

def build_repo_map(root, *, max_depth=3, per_dir_limit=20,
                   budget_tokens=800, ignore=None) -> RepoMap: ...
def render_repo_map(repo_map: RepoMap, budget_tokens: int) -> str: ...
```

**符号抽取**：`.py` 用 `ast.parse`（失败则跳过该文件，不抛），取模块 docstring 首行 + 顶层 `ClassDef`/`FunctionDef`/`AsyncFunctionDef` + 类内一层方法；`end_lineno` 在 3.8+ 可用，直接取。其它扩展名用 [moss/features/memory.py:571](moss/features/memory.py#L571) 的行首前缀表匹配。二进制判定：前 1KB 含 `\x00` 即跳过；单文件 >1MB 跳过符号抽取但仍进目录树。

**排序**（决定预算不够时先保留谁）：
1. 入口文件（`__main__.py`、`cli.py`、`main.*`、`index.*`）；
2. `size * recency_weight`，`recency_weight = exp(-Δdays / 30)`；
3. 路径字典序（保证确定性）。

**缓存**：`.moss/cache/repo_map.json`，`cache_key = sha256(HEAD mtime_ns, .git/index mtime_ns, 各顶层目录 mtime_ns, ignore 文件内容 hash, REPO_MAP_SCHEMA_VERSION)`。key 不变直接反序列化返回。

### 4.2 `workspace.py` 的改动

```python
@dataclass(frozen=True)
class DocRef:
    path: str
    preview: str        # 进 prompt 的结构摘要
    digest: str         # 对全文算的 sha256 —— 指纹用这个，不用 preview
    total_lines: int
    truncated: bool

@dataclass(frozen=True)
class GitFacts:
    branch: str
    default_branch: str
    status_entries: tuple[str, ...]
    counts: dict          # {"modified": 12, "added": 3, "deleted": 1, "untracked": 5}
    recent_commits: tuple[str, ...]
    collected_at: float
    cache_key: str        # (.git/index mtime_ns, .git/HEAD mtime_ns)

def collect_git_facts(cwd, *, ttl_s=0.5, cache={}) -> GitFacts: ...
```

- 子进程从 4 个降到 2 个：`git status --short --branch --untracked-files=normal`（一次拿分支 + 状态）与 `git log --oneline -5`。`rev-parse --show-toplevel` 只在没有缓存时跑，`symbolic-ref origin/HEAD` 只在 cache miss 时跑。
- `WorkspaceContext.build(cwd, *, repo_root_override=None, git_facts=None, ignore=None)`；`WorkspaceContext` 增加 `invocation_cwd` 与 `repo_root` 两个独立字段。
- `text()` 里 status 段先渲染 `modified: 12 · added: 3 · deleted: 1 · untracked: 5`，再列**按当前任务相关度排序**的前 20 条路径（无任务上下文时按状态字母序，保证确定性）。
- `fingerprint()` 改用 `DocRef.digest` + 完整 status 计数 + 全部 status 路径的 sha256，**不再对 preview 取 hash**。

**文档发现**（`discover_docs(repo_root, cwd, extra_names=())`）：
- 默认名单：`AGENTS.md`、`CLAUDE.md`、`CONTRIBUTING.md`、`README*`、`pyproject.toml`、`package.json`、`Makefile`、`justfile`；
- 可用 `.moss/config.json` 的 `repo_context.doc_names` 覆盖；
- 超过 preview 预算时 preview = "标题树（`#`/`##` 行）+ 前 N 行 + `（全文 N 行，用 read_file <path> 取回）`"。

**分层就近注入**：`tool_executor` 在 `read_file` / `edit_file` / `write_file` 成功后，检查目标路径的祖先目录里是否存在未注入过的 `AGENTS.md`；有则追加一条 runtime notice（同一 run 内每份文档只注一次），并落 trace `instruction_loaded{path, scope}`。同名规则冲突时最近目录优先，落 `instruction_conflict{path, winner}`。

### 4.3 快照增量化

```python
def capture_snapshot(root, *, ignore=None, strategy="auto", git_changed=None) -> dict: ...
```

- `strategy="git"`：`git status --porcelain -z --untracked-files=all` 拿变更集，只对变更路径 `lstat`；
- `strategy="walk"`：现有全量遍历（无 git 或 git 调用失败时的退路）；
- `strategy="auto"`：探测到 `.git` 且 `git` 可执行则用 git，否则 walk；
- 两条路径都改用 `os.lstat`，符号链接记 `("symlink", target_hash)` 而不是被跳过；
- 目录级剪枝：`IgnoreRules` 命中的目录不再下钻；`MOSS_SNAPSHOT_EXCLUDE` 可追加。
- **已知边界**：同尺寸覆盖写且 mtime 被还原时 `(mtime_ns, size)` 判不出来，靠 git 变更集兜底；两条路径都不可用时在 report 里记 `snapshot_strategy="walk_only"`，评测口径要能看见。

### 4.4 上下文注入位置

| 内容 | 放哪 | 预算归属 | 理由 |
| --- | --- | --- | --- |
| repo map | workspace 段（prefix 尾部） | prefix | 与任务无关、run 内稳定，进稳定缓存段（`stable_hash` 不覆盖 workspace，不影响 cache key） |
| `Likely relevant files:` | `relevant_memory` 段 | relevant_memory | 与当前请求相关，每轮可能变，不能进缓存段 |
| 就近 `AGENTS.md` | runtime notice → history | history | 事件性质，append-only（见 [spec-04](spec-04-prompt-cache.md) §4.2） |

### 4.5 配置项

| key | `.env` / 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `repo_context.repo_map` | `MOSS_REPO_MAP` | `on` | 总开关 |
| `repo_context.repo_map_budget` | `MOSS_REPO_MAP_BUDGET` | `800` | token |
| `repo_context.snapshot_strategy` | `MOSS_SNAPSHOT_STRATEGY` | `auto` | `git`/`walk`/`auto` |
| `repo_context.snapshot_exclude` | `MOSS_SNAPSHOT_EXCLUDE` | `""` | 逗号分隔 glob |
| `repo_context.git_ttl_ms` | `MOSS_GIT_TTL_MS` | `500` | git 采集缓存 TTL |
| `repo_context.doc_names` | — | 见 4.2 | 仅 `.moss/config.json` |

### 4.6 涉及文件

| 文件 | 改动 |
| --- | --- |
| `moss/ignore.py` | 新增 |
| `moss/repo_map.py` | 新增 |
| [moss/workspace.py](moss/workspace.py) | `DocRef`/`GitFacts`/`discover_docs`/`collect_git_facts`；`build` 签名扩展；`fingerprint` 改用全文 digest；`capture_snapshot` 增量化 + lstat |
| [moss/runtime.py:236](moss/runtime.py#L236) | 保存 `invocation_cwd`，refresh 用它；把 repo map 挂进 workspace 段 |
| [moss/context_manager.py](moss/context_manager.py) | `relevant_memory` 段渲染加 `Likely relevant files` 分组 |
| [moss/tool_executor.py](moss/tool_executor.py) | 文件类工具成功后触发就近文档注入；快照调用传 `ignore` |
| [moss/prompt_prefix.py](moss/prompt_prefix.py) | 无需改（repo map 在 workspace 段，`stable_hash` 天然不覆盖） |
| `moss/trace_events.py` | 新事件常量：`instruction_loaded`、`instruction_conflict`、`repo_map_built`、`anchor_miss` |

## 5. 兼容与迁移

- `WorkspaceContext.build(cwd)` 单参调用保持可用（新参数全为关键字且有默认值）。
- `fingerprint()` 口径变化会让所有既有 checkpoint 的 `workspace_fingerprint` 不匹配 → 首次运行表现为 `workspace-mismatch`。**处理方式**：`WORKSPACE_FINGERPRINT_VERSION` 进指纹前缀，`checkpoint.evaluate_resume_state` 遇到旧版本前缀时返回 `schema-mismatch` 而不是 `workspace-mismatch`（语义更准，且已有分支能处理）。
- `.moss/cache/` 是新目录，需确认已被 `.gitignore` 的 `.moss/` 覆盖。
- repo map 关掉（`MOSS_REPO_MAP=off`）时行为与现在完全一致，作为回退开关。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| `tests/test_ignore.py` | 注释 / 取反 / 目录尾斜杠 / `**` / 前导 `/`；与 `git check-ignore` 在本仓库上抽样一致 |
| `tests/test_repo_map.py` | 语法错误的 .py 不抛异常；符号行号正确；预算内 token ≤ 上限；缓存命中不重建（mock 计时）；排序确定性（跑两次逐字节一致） |
| `tests/test_workspace.py`（扩展） | 改 README 第 2000 字符 → `fingerprint()` 变化；子目录启动连续两轮 cwd 与指纹稳定；git 采集 2 次子进程（mock `subprocess.run` 计数）；status 结构化摘要格式 |
| `tests/test_snapshot.py` | 新建/删除/改写/改权限均被检出；符号链接指向仓库外被记为变更；`strategy="git"` 与 `strategy="walk"` 结果一致（同一 fixture 上取交集断言） |
| `tests/test_safety_invariants.py`（扩展） | 增量快照下路径逃逸/软链逃逸仍被拒；新增"同尺寸改写"用例 |
| `tests/test_moss.py`（扩展） | 就近 `AGENTS.md` 在触碰该目录后注入且只注一次 |

## 7. 验收标准

| 指标 | 门槛 |
| --- | --- |
| 定位阶段步数（首次命中目标文件前的工具步数）中位数 | 相对基线下降 ≥40% |
| repo map 段 | ≤800 token；构建 <200ms，缓存命中 <5ms |
| `prompt_built.duration_ms` P50 | 下降 ≥60%；5000 文件仓库 <50ms |
| risky 工具 `tool_executed.duration_ms` P95（10k 文件仓库） | 下降 ≥50% |
| prefix 文档段 token | 下降 ≥30% |
| `anchor_miss` 率 | <50%（超过即关闭 1.4 的起点锚） |

## 8. 实施顺序（PR 拆分）

1. **PR-1（P0，S）**：`fingerprint` 用全文 digest + `invocation_cwd` 修正 + 指纹版本前缀。含迁移分支与测试。
2. **PR-2（P0，S）**：`collect_git_facts` 合并采集 + TTL 缓存 + status 结构化摘要。
3. **PR-3（P1，S）**：`moss/ignore.py` + 快照增量化 + lstat。
4. **PR-4（P1，M）**：`moss/repo_map.py` + 缓存 + 注入 workspace 段。
5. **PR-5（P1，S）**：文档分层发现 + 结构摘要 + 就近注入。
6. **PR-6（P1，M）**：`Likely relevant files` 起点锚（依赖 [spec-05](spec-05-memory.md) 的 BM25，可先用词频版并标 TODO）。

## 9. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| repo map 排序不准，把模型带偏 | `anchor_miss` 指标 + `MOSS_REPO_MAP=off` 一键回退；PR-4/PR-6 分开上线，能单独回滚 |
| 手写 gitignore 匹配器与 git 语义有出入 | 只用于"少扫一些文件"和"少展示一些文件"，安全判定（路径锚定）不依赖它；测试里与 `git check-ignore` 抽样对拍 |
| git 缓存 TTL 导致 status 过期 | TTL 仅 500ms，且 risky 工具执行后强制失效 |
| 指纹口径变化让所有旧 checkpoint 失效 | 版本前缀 + `schema-mismatch` 分支，不产生误导性的 `workspace-mismatch` |

## 10. 开放问题

1. repo map 是否应该在 `read_file` 大量发生后自动收缩（模型已经认路了，地图就是纯开销）？倾向：先不做，用 6.5 的 `section_share` 观察一轮再定。
2. 就近 `AGENTS.md` 的注入是否该计入 `attempts`？倾向：不计，它不是模型的一轮决策。
3. 多 worktree / submodule 的识别暂不做，但 `repo_id` 字段先预留（`repo_root` 的 realpath hash），避免将来加时改 schema。
