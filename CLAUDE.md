# CLAUDE.md

moss 是一个轻量本地 coding agent：一个包在模型外面的控制循环，负责组 prompt、解析模型输出、校验并执行工具、写 trace/report、维护跨轮记忆。目标是"轻量好用"，刻意保持零第三方运行时依赖（HTTP 用 stdlib `urllib`，`.env`/frontmatter 都是手写解析器，不引入 requests/YAML）。

## 常用命令

```bash
python -m pytest tests/ -q        # 全量测试，约 4 分钟
python -m pytest tests/test_moss.py -q -k "pattern"   # 单点调试
pip install -e .                  # 安装后可直接用 `moss` 命令
python -m moss                    # 模块入口（等价于 moss）
uv run ruff check moss tests scripts   # lint（本机 base 环境没装 ruff，用 uv）
```

**测试基线（Windows 本机）：全量通过、仅 4 个失败即为绿色。** 那 4 个失败全部是 Windows 环境问题，不是代码 bug，只需要关注新增失败：
- `test_evaluator.py` ×3：benchmark verifier 需要真实环境/`python3`
- `test_safety_invariants.py::test_symlink_path_traversal_is_rejected`：Windows 创建符号链接需要特权（WinError 1314）

CI（`.github/workflows/ci.yml`）在 Ubuntu 上跑同样的 `ruff check` + `pytest`（Python 3.10 与 3.12），那里应当全绿；CI 出现任何失败都必须排查，不允许 skip 了事。

Git 约定：个人项目，直接提交并 push 到 `main`，不开分支/PR。push 偶发 TLS 报错，重试即可。

## 架构地图

一次 `ask()` 的数据流：

```
cli.py (装配/REPL/进度渲染)
  └─ runtime.py::Moss (facade：所有状态和护栏都挂在这里)
       └─ agent_loop.py::AgentLoop.run (感知→决策→行动→记录 主循环)
            ├─ context_manager.py   每轮按预算组 prompt（prefix/memory/relevant/history/request 五段）
            ├─ providers/clients.py 统一 complete() 接口（Ollama / OpenAI /responses / Anthropic /messages）
            ├─ output_parser.py     模型输出 → ("tool"|"final"|"retry", payload)（纯函数）
            ├─ tool_executor.py     执行护栏：allowlist→存在性→校验→重复检测→审批→快照 diff
            │    └─ tools.py        工具白名单（显式注册，非动态发现）
            ├─ checkpoint.py        每步落 checkpoint（上限 CHECKPOINT_HISTORY_LIMIT=40，自动裁剪）
            └─ run_store.py         .moss/runs/<run_id>/{task_state.json, trace.jsonl, report.json}
```

支撑模块：
- `workspace.py`：仓库快照（git 事实 + 分层发现的项目文档 + 文件级 `(mtime_ns, size)` 快照/diff），进 prompt prefix。
  git 采集走 `collect_git_facts`（status+log 两个子进程 + 500ms TTL 缓存，risky 工具后强制失效）；
  快照默认 `strategy="auto"`：有 git 就只对变更集 lstat，否则全量 walk。
  `fingerprint()` 带 `WORKSPACE_FINGERPRINT_VERSION` 前缀，且用文档**全文** digest（不是被裁剪的 preview）
- `ignore.py`：手写 `.gitignore` 匹配器。快照与 repo map 共用同一套忽略口径，
  **安全判定（路径锚定）不依赖它**，只用来少扫/少展示
- `repo_map.py`：目录骨架 + 符号索引（Python 走 stdlib `ast`，其它语言走行首前缀），
  缓存在 `.moss/cache/repo_map.json`；`rank_relevant_files` 给出每轮的 `Likely relevant files` 起点锚
- `trace_events.py`：trace 事件名常量（`instruction_loaded` / `instruction_conflict` / `repo_map_built` / `anchor_miss`）
- `token_budget.py`：token 估算与全部文本裁剪（`clip_to_budget` 按预算二分；`clip`/`middle` 硬切片，`MAX_TOOL_OUTPUT=16000`、`MAX_HISTORY=32000`）；`clock.py`：统一 UTC 时间戳 `now()`
- `output_parser.py`：模型输出 → `("tool"|"final"|"retry", payload)` 的纯函数解析层
- `prompt_prefix.py`：稳定前缀构建。**prompt cache key 用 `stable_hash`（只覆盖身份/规则/Tools/Skills 段），不用整段 hash**——否则 agent 自己写文件会导致 workspace 段变化、缓存键每轮抖动
- `features/memory.py`：分层记忆（working / episodic notes / durable topics），文件摘要带 freshness 失效；也承载记忆写入/durable 提炼策略（`update_memory_after_tool`/`extract_durable_promotions` 等，Moss 只薄委托）
- `session_store.py`：会话持久化到 `.moss/sessions/`；delegate 子 agent 的会话隔离在 `.moss/delegates/`（不能污染 `--resume latest`）
- `security.py`：secret 检测/脱敏；`run_shell` 只继承 `DEFAULT_SHELL_ENV_ALLOWLIST` 里的环境变量（**含 Windows 必需的 COMSPEC/SYSTEMROOT 等，删了 run_shell 在 Windows 上直接崩**）
- `config.py`：`.env` 加载（坏行跳过并警告，不允许让整个启动崩掉）；`.moss/config.json` 装结构化配置（如 `repo_context.doc_names`）
- `skills.py`：`.moss/skills/*.md`（frontmatter: name/description，正文按 `use_skill` 懒加载）
- `evaluation/`：benchmark 与 ablation，不属于运行时路径

公共 API 只从 `moss/__init__.py` 导出；旧的 `moss.evaluator`/`moss.metrics`/`moss.models`/`moss.memory` 平铺模块已删除，不要复活它们。

## 关键约定与不变量

1. **持久化必须原子写**：先写同目录临时文件再 `os.replace`（见 `SessionStore.save` / `RunStore._write_json_atomic`）。session 每次 record 都整份重写，非原子写会在断电/Ctrl-C 时丢整个会话。
2. **CLI 输出契约**：最终答案走 stdout（可管道），进度/警告/错误走 stderr。进度通过 `agent.progress_observer` 钩子（`emit_progress`），observer 异常必须被吞掉，绝不影响控制流。
3. **错误收敛，不裸抛**：模型后端异常由 `AgentLoop._finish_model_error` 收敛为已收尾的失败运行（task_state=failed / stop_reason=model_error，trace+report 齐全），并对错误信息脱敏。one-shot 模式下失败必须非零退出（CI 依赖这个）。`KeyboardInterrupt` 继承 BaseException 不会被捕获——REPL 里 Ctrl-C 只取消当前轮。
4. **所有落盘/展示的文本先过脱敏**：`redact_artifact` / `redact_text`，secret 名单来自 `DEFAULT_SECRET_ENV_NAMES` + `MOSS_SECRET_ENV_NAMES` + `--secret-env-name`。
5. **路径锚定**：所有文件类工具经 `Moss.path()`，resolve 后必须在 workspace root 之下（防 `../` 和符号链接逃逸）。遍历工作区时不跟随符号链接目录（防死循环）。
6. **工具是显式注册的白名单**（`BASE_TOOL_SPECS`），risky 工具走审批（`ask`/`auto`/`never`）；审批提示只展示摘要（`tool_executor.approval_summary`：写文件类展示脱敏 diff、shell 展示风险分级），不 dump 完整 args。
7. **快照 diff 用 `(mtime_ns, size)`**，不做内容 hash——risky 工具每次调用前后各扫一遍工作区，性能敏感。已知盲区：walk 策略下同尺寸覆盖写 + mtime 还原判不出来（git 策略靠变更集兜底），chmod 也不可见。
8. **注释风格**：中文、解释"为什么存在/在链路里的位置"，新代码保持一致。

## 配置

优先级：`显式 CLI 参数 > .env 里的 MOSS_* > 旧环境变量 > 代码默认值`。`.env` 在构建 provider client 前加载并覆盖进程环境变量（解析器只读字面量，不支持 `$VAR` 展开）。

默认 provider 是 `deepseek`。各 provider 真实默认模型（改动时必须同步 `cli.py`、`evaluation/metrics.py`、`.env.example`、README 和相关测试断言，历史上这里出现过四处编造的模型名）：

| provider | 默认模型 | 协议 |
| --- | --- | --- |
| deepseek | `deepseek-v4-pro` | Anthropic-compatible `/messages` |
| openai | `gpt-5-5` | OpenAI-compatible `/responses` |
| anthropic | `claude-opus-5` | Anthropic-compatible `/messages` |
| ollama | `qwen3:8b` | Ollama `/api/generate` |

仓库上下文相关的开关（见 `.env.example`）：`MOSS_REPO_MAP`（默认 on，一键回退）、`MOSS_REPO_MAP_BUDGET`（默认 800 token）、`MOSS_SNAPSHOT_STRATEGY`（`git`/`walk`/`auto`，默认 auto）、`MOSS_SNAPSHOT_EXCLUDE`（逗号分隔 glob）。

CLI 默认：`--max-steps 25`、`--max-new-tokens 4096`、`--approval ask`。ContextManager 预算以**估算 token** 为单位（见 `token_budget.py`，CJK 约 1 char/token、拉丁约 4 chars/token；测试可注入 `measure=len` 走字符级以稳定断言）：总预算 12000 token（section 预算 prefix 3000 / memory 1000 / relevant 800 / history 6000）。

## 本地状态目录（全部 gitignored）

```
.moss/sessions/    用户会话（--resume latest 按 mtime 取最新）
.moss/cache/       repo map 等派生缓存（可随时删，会自动重建）
.moss/delegates/   delegate 子 agent 的一次性会话（隔离，防污染 latest）
.moss/runs/<id>/   单次运行审计工件：task_state.json / trace.jsonl / report.json
.moss/skills/      技能 markdown
.env               本地密钥（仓库只保留 .env.example）
```
