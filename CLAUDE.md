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
            ├─ context_manager.py   每轮按预算组 prompt（prefix/history/memory/relevant/constraints/request 六段）+ admission gate
            ├─ providers/clients.py 统一 complete() 接口（Ollama / OpenAI /responses / Anthropic /messages）
            ├─ output_parser.py     模型输出 → ("tool"|"final"|"retry", payload)（纯函数）
            ├─ tool_executor.py     执行护栏：allowlist→存在性→校验→重复检测→审批→快照 diff→大输出卸载
            │    └─ tools.py        工具白名单（显式注册，非动态发现）
            ├─ compaction.py        上下文压缩：结构化交接（可逆/幂等/闭合），默认 off
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
- `trace_events.py`：trace 事件名常量（就近文档、repo map、批执行、计划、停滞、预算、中断）
- `stall.py`：四类停滞检测（`repeat_exact` / `ab_loop` / `no_progress` / `error_storm`），命中后注入**结构化**干预而不是拒绝执行
- `budget.py`：`RunBudget` 多维预算（步数 / token / 时间 / 金额），软阈值 80% 提醒收敛、硬阈值不再调模型直接优雅收尾。
  `usd=None` 表示"不知道价格"，**绝不能当成 0**
- `verification.py`：判断一次 `run_shell` 算不算"跑过验证"。收尾自检和评测的 `unverified_edit_rate` 必须共用它
- `shell_policy.py`：shell 风险分级。**基于 shlex 的结构化解析**（按 `; && || |` 和引号外换行拆段，逐段看 argv[0]），
  六档 `read_only/test/write/network/high/denied`。`denied` 连审批都不给；命令替换/eval/引号不闭合一律 `high` + `undecidable`
- `policy.py`：能力标签（`fs_read/fs_write/exec/network/spawn/memory_write`）+ 路径 glob 作用域。
  **fail-closed**：risky 但未声明能力的工具直接拒绝
- `injection.py`：工具输出里的 prompt injection 检测。命中**只收紧策略不拒绝执行**（本 run 剩余 risky 工具强制审批）
- `sandbox.py`：L1 策略层 / L2 `sandbox-exec`·`bwrap` / L3 容器。**任何降级都要进 report 且打 stderr**
- `token_budget.py`：token 估算与全部文本裁剪（`clip_to_budget` 按预算二分；`clip`/`middle` 硬切片，`MAX_TOOL_OUTPUT=16000`、`MAX_HISTORY=32000`）；
  另含**在线校准**：`.moss/cache/token_calibration.json` 按 `(provider, model)` 分桶存最近 50 条 `(估算, 后端真值)`，
  样本 <5 时 ratio 固定 1.0，偏差 >30% 告警 `token_estimate_drift` 并退回 1.0；探测到 `tiktoken` 就用真值（import 探测，不进依赖）
- `output_compressors.py`：按**输出形状**（不只是工具名）注册的压缩器——`pytest`/`lint`/`search_text`/`git_diff`/`list_files` + `generic` 兜底。
  只有已落盘成 artifact 的输出才压缩（有损只在"完整版能取回"时才可接受）；`exit_code` 行永远置顶不被切掉；
  原文有失败信号而压缩后一条不剩时打 `error_signal_lost`
- `compaction.py`：把较早历史压成结构化交接（goals/completed/excluded/findings/open_questions/plan）。
  四条硬性质：**可逆**（原文写 `.moss/runs/<id>/context/turns-N.jsonl`，`read_artifact` 可取回）、
  **幂等**（同输入同 method 逐字段一致；压过的区间不再产新 artifact）、**闭合**（covered + kept = 全集）、
  **因果单元不可拆**（有调用无结果的组一律留在尾巴里，最近 3 步原样保留）。
  模型模式的三道校验都是代码级的：`completed` 只能从规则模式的集合里选、证据锚点必须是规则模式见过的、解析失败退回 rule 并如实记 method
- `clock.py`：统一 UTC 时间戳 `now()`
- `output_parser.py`：模型输出 → `("tool"|"final"|"retry", payload)` 的纯函数解析层
- `prompt_prefix.py`：稳定前缀构建。**prompt cache key 用 `stable_hash`（只覆盖身份/规则/Tools/Skills 段），不用整段 hash**——否则 agent 自己写文件会导致 workspace 段变化、缓存键每轮抖动
- `model_request.py`：结构化 `system blocks + messages` 请求；仓库/工具内容永不进入 system。provider 支持 native tool 时直接保留全部 `call_id`
- `providers/capabilities.py`：按 provider/model prefix 显式声明 cache/native/context 能力；未知模型保守关闭缓存，不再按 URL 猜测
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
3. **一轮可以有多个动作**：`parse_model_actions` 按出现位置解析多个 `<tool>` 块，`final` 之后的一律丢弃（记 `batch_truncated`）。
   **顺序不变量**：写回 history/trace 的顺序恒等于 `Action.index`，不是完成顺序——录制回放依赖这条。
   只读工具批可并发（`--parallel-tools`，默认 off），并发阶段只做纯执行，memory/record/trace 回主线程按序补做。
4. **错误收敛，不裸抛**：模型后端异常由 `AgentLoop._finish_model_error` 收敛为已收尾的失败运行（task_state=failed / stop_reason=model_error，trace+report 齐全），并对错误信息脱敏。one-shot 模式下失败必须非零退出（CI 依赖这个）。`KeyboardInterrupt` 继承 BaseException 不会被捕获——REPL 里 Ctrl-C 只取消当前轮。
5. **所有落盘/展示的文本先过脱敏**：`redact_artifact` / `redact_text`，secret 名单来自 `DEFAULT_SECRET_ENV_NAMES` + `MOSS_SECRET_ENV_NAMES` + `--secret-env-name`。
6. **路径锚定**：所有文件类工具经 `Moss.path()`，resolve 后必须在 workspace root 之下（防 `../` 和符号链接逃逸）。遍历工作区时不跟随符号链接目录（防死循环）。
   **唯一执行入口是 `run_tool` / `execute(ActionRequest)`**；`Moss` 上不允许再出现绕过 `ToolExecutor` 的公共 `tool_*` 方法（有契约测试守着）。
   审批与写入之间用 `ApprovalReceipt` + `expected_sha` 挡 TOCTOU：审批后文件被换掉就 `precondition_failed`，要求重新审批。
7. **工具是显式注册的白名单**（`BASE_TOOL_SPECS`），risky 工具走审批（`ask`/`auto`/`never`）；审批提示只展示摘要（`tool_executor.approval_summary`：写文件类展示脱敏 diff、shell 展示风险分级），不 dump 完整 args。
8. **快照 diff 用 `(mtime_ns, size)`**，不做内容 hash——risky 工具每次调用前后各扫一遍工作区，性能敏感。已知盲区：walk 策略下同尺寸覆盖写 + mtime 还原判不出来（git 策略靠变更集兜底），chmod 也不可见。
9. **中断也要留全工件**：`AgentLoop.run` 外层 `except BaseException` 只做收尾（`stop_reason=interrupted` + trace + report）然后**必然重新抛出**；收尾函数自己绝不抛异常。
10. **超预算就不许发**（admission gate）：`ContextManager.build_result()` 给出 `sendable` / `overflow_reason`
    （`request_too_large` / `prompt_too_large`）。装不下时的顺序是：先 compaction 重算 → 再把当前请求本身卸载成 artifact + 指针 →
    仍超才**不调用 provider**，收敛成 `stop_reason=context_overflow` 的失败运行（one-shot 退出码非 0）。
    **feature flag 只能换策略，不能关掉这道闸**：`context_reduction=off` 时依然生效。
11. **截断必须可逆**：超过 `ARTIFACT_THRESHOLD=4000` 字符的工具输出落进 `.moss/runs/<id>/artifacts/`（按内容 sha12 去重、脱敏后写），
    prompt 里只放压缩摘要 + `read_artifact` 指针。`read_artifact` 的 `path_scope="run_dir"`——用 `Moss.path()` 会放行整个仓库，
    那等于多开一条绕过 `read_file` 的读文件通道。report 里的 `truncated_bytes_lost` 应恒为 0。
12. **注释风格**：中文、解释"为什么存在/在链路里的位置"，新代码保持一致。

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

安全开关：`--sandbox`（默认 auto）、`--allow-network`、`--allow` / `--deny`（能力+glob）、`--injection-scan`（默认 on）。

主循环开关：`--parallel-tools`（默认 off）、`--verify-before-final`（默认 on）、`--max-input-tokens` / `--max-output-tokens` / `--max-seconds` / `--max-usd`（默认全 None，不设即老行为）。

提示词开关：`--tool-protocol=auto|native|text`（默认 auto）、`--context-mode=rerender|append_only`（默认 rerender）、`--no-prompt-cache`。内置 prompt 版本是 `p1`；`.moss/prompts/system.md` 可覆盖稳定 system head，版本记为 `file:<sha256前12位>` 并写入 report/run_manifest。

上下文开关：`--compaction=off|rule|model`（默认 off = 今天的纯截断行为，也是消融基线）、`MOSS_CONTEXT_RATIO`（默认 0.5）、`MOSS_CONTEXT_HARD_CAP`（默认 60000）。

CLI 默认：`--max-steps 25`、`--max-new-tokens 4096`、`--approval ask`。ContextManager 预算以**估算 token** 为单位（见 `token_budget.py`，CJK 约 1 char/token、拉丁约 4 chars/token；测试可注入 `measure=len` 走字符级以稳定断言）。
总预算**按模型窗口推导**：`min(context_window * ratio, hard_cap) - max(max_new_tokens, 1024)`；
`ModelCapabilities.known` 为假（认不出来的模型）时退回历史值 12000——拿猜出来的窗口去放大预算，撞的是 provider 的 context-length 报错。
各段按占比切（prefix 35% / history 45% / memory 8% / relevant 7% / constraints 5%），编辑期把 5% 从 history 挪给 relevant+constraints。

## 本地状态目录（全部 gitignored）

```
.moss/sessions/    用户会话（--resume latest 按 mtime 取最新）
.moss/cache/       repo map、token 校准等派生缓存（可随时删，会自动重建）
.moss/delegates/   delegate 子 agent 的一次性会话（隔离，防污染 latest）
.moss/runs/<id>/   单次运行审计工件：task_state.json / trace.jsonl / report.json
                   artifacts/ 卸载的大工具输出（read_artifact 按行取回）
                   context/   被 compaction 压掉的原始历史 turns-N.jsonl
.moss/skills/      技能 markdown
.env               本地密钥（仓库只保留 .env.example）
```
