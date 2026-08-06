# CLAUDE.md

moss 是一个轻量本地 coding agent：一个包在模型外面的控制循环，负责组 prompt、解析模型输出、校验并执行工具、写 trace/report、维护跨轮记忆。目标是"轻量好用"，刻意保持零第三方运行时依赖（HTTP 用 stdlib `urllib`，`.env`/frontmatter 都是手写解析器，不引入 requests/YAML）。

## 常用命令

```bash
uv run --with pytest python -m pytest tests/ -q
python -m pytest tests/test_moss.py -q -k "pattern"   # 单点调试
pip install -e .                  # 安装后可直接用 `moss` 命令
python -m moss                    # 模块入口（等价于 moss）
uv run ruff check moss tests scripts   # lint（本机 base 环境没装 ruff，用 uv）
```

**测试基线：全量测试必须零失败。** 环境差异应显式归类或跳过，不能把固定失败数当成绿色。

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
            ├─ model_router.py      脏活（compaction/反思/judge）走 aux model，主线走主模型
            ├─ hooks.py             用户钩子 pre_tool/post_tool/pre_final/post_run
            ├─ checkpoint.py        每步落 checkpoint（上限 CHECKPOINT_HISTORY_LIMIT=40，自动裁剪）
            ├─ action_ledger.py     risky 动作的 intent/receipt 两阶段 + 崩溃后对账
            └─ run_store.py         .moss/runs/<run_id>/{task_state.json, trace.jsonl, report.json, lease.json}
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
- `trace_events.py`：trace 事件名常量 + `TRACE_SCHEMA_VERSION` + `ALL_EVENTS`。
  **禁止在别处写事件名字面量**（`tests/test_trace_events.py` 用 AST 扫 `moss/evaluation/` 和所有
  `emit_trace`/`append_trace` 调用；trace 里出现的名字也必须在 `ALL_EVENTS` 内）
- `atomic_io.py`：原子 + 持久落盘。`write_atomic` 的顺序不可换（临时文件 → fsync → replace → fsync 目录）；
  jsonl 追加是**每条 flush、每 20 条 fsync**（flush 扛进程被杀，fsync 防断电，摊派是刻意的取舍）；
  目录 fsync 在 Windows 上不可用 → 记降级并进 `report.durability_degradations`，不假装持久。
  `truncate_partial_tail` 修崩溃留下的半截行——不修的话下一条记录会被粘上去，两条一起读不出来
- `lease.py`：run 租约（PID + host + boot_id + 心跳 + TTL 90s）。判定链**保守**：跨机只看 TTL、
  机器重启过判死、同机探 `os.kill(pid,0)`、探到活但心跳超 TTL 判死，其余判活。
  最坏是"该接管的没接管"，绝不误杀活 run。长模型调用/大 pytest 期间由 `LeaseHeartbeat` 独立线程续租
- `run_index.py`：`.moss/runs/index.jsonl`（append-only，读时按 run_id 折叠）+ 保留策略。
  启动只读索引，不再 glob 全部 `task_state.json`。过期 run 打包成 `<run_id>.jsonl.gz`（逐文件 jsonl，仍可 grep）；
  **永不清理** pinned / 持有有效租约 / 被 `artifacts/*.json` 引用的 run
- `action_ledger.py`：risky 工具执行前后各落一条 `action_intent` / `action_receipt`。
  `intended_sha`（这次动作做完文件**应该**是什么）是自动对账的前提；
  恢复时缺 receipt 的动作：幂等工具比指纹判"已生效/可重放"，**非幂等一律 `pending_unknown` 不自动重放**
- `rewind.py`：`/rewind [n]` 同时回退文件 + history + memory 快照。
  用户在 agent 改完之后又动过的文件 → 整个停下等确认，一个字节都不动（`/rewind!` 强制）
- `otel.py`：`moss runs export --otel`，stdlib 生成 OTLP/JSON，只落文件不推 collector
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
- `providers/recording.py`：确定性录制回放（spec-09 §9.8）。`RecordingModelClient` / `ReplayModelClient`
  是**包在真实 client 外面的装饰器**，对主循环透明；身份属性（provider/model/capabilities）必须透传。
  `request_fingerprint` 规范化剔除时间戳、run/session id、workspace 绝对路径前缀、长 hex、耗时——
  取舍是**宁可 miss 也不撞车**（miss 有 `on_miss` 兜底并告警，撞车是静默回放出错误的回答）。
  磁带落盘前过 `redact_artifact`（磁带进 git）。`on_miss=fail` 是 CI 默认
- `delegation.py`：子 agent 契约（spec-09 §9.1）。`DelegateContract.context_seed` 由父 agent
  **显式构造**（任务目标 + 相关文件路径），不再截断父 history；`DelegateResult.findings` 带证据锚点，
  父 agent 可直接 `read_file` 核验。能力必须是父集的子集且不越只读边界（fail-closed）。
  锚点指不到工作区内的文件就丢掉锚点只留结论——**假锚点比没锚点更糟**
- `model_router.py`：多模型路由（spec-09 §9.7）。`AUX_TASKS` 显式列出脏活；`bind()` 给子系统
  一个只认 `complete()` 的门面，aux 失败自动回落主模型并记 `aux_degraded`。
  **未配置 aux 时行为与加路由前逐字节一致**（这是消融基线）；aux 的输出不进主线 history
- `mcp/`：MCP 客户端与服务端（spec-09 §9.2），JSON-RPC 2.0 over stdio 手写。
  外部工具在**启动期**转成 `ToolSpec` 落白名单并进 `tool_signature`，**不做运行期动态发现**；
  必须在 `.moss/config.json` 声明 capabilities（fail-closed），一律加隐含 `network` + risky。
  server 侧走 `Moss.execute(ActionRequest)` 唯一入口，默认只导出只读工具
- `hooks.py`：用户钩子（spec-09 §9.5）。`.moss/hooks/<point>`，只认可执行位，超时 3s、失败不阻断。
  **唯一能改控制流的是 `pre_tool` 的退出码 2**，且必须记 `hook_denied`。钩子拿到的是脱敏后的 JSON。
  agent 写不进 `.moss/`（`policy.DEFAULT_DENY` 已覆盖），否则它能给自己装后门
- `code_mode.py`：受限 Python 编排（spec-09 §9.3）。**默认关闭 + 沙箱硬前置**。
  节点类型 / 属性名 / 自由名字**三层全部白名单**——只做节点白名单挡不住 `eval(...)`，
  那在 AST 上就是普通的 `Call(Name)`。每次工具调用仍逐条走 `ToolExecutor`
- `trace_html.py`：`moss runs show <id> --html`，单文件、内联 CSS/SVG、**零外部请求**；
  工具输出进 HTML 前一律转义（它是不可信数据）
- `evaluation/`：benchmark 与 ablation，不属于运行时路径
  - `cassettes.py`：L1 磁带按 `benchmarks/cassettes/<prompt_version>/<task_id>/` 存放；
    `UNCASSETTABLE_TASKS` 登记了两条**做不到**指纹稳定的任务及原因
  - L0–L4 证据不得跨层混写；scripted 入口只属于 L1，不能声称模型能力
  - L2/L3 必须使用临时 workspace、RunManifest、成本字段与统计区间；infra failure 单列
  - judge 不能决定 binary pass；公开 adapter 不负责下载数据或宣称榜单分数

磁带 `benchmarks/cassettes/<prompt_version>/<task_id>/` **进 git**（小、脱敏过、CI 要用）；
录制走 `scripts/record_cassettes.py`，manifest 里的 `source` 区分 `scripted-bootstrap` 与 `provider`——
从脚本引导出来的磁带不能声称是真实模型轨迹。

公共 API 只从 `moss/__init__.py` 导出；旧的 `moss.evaluator`/`moss.metrics`/`moss.models`/`moss.memory` 平铺模块已删除，不要复活它们。

## 关键约定与不变量

1. **持久化必须原子写**：一律走 `atomic_io.write_atomic`（临时文件 → fsync → `os.replace` → fsync 目录），
   不要在别处再手写一份 tmp+replace。`os.replace` 只保证"不出现半截文件"，不保证落盘——
   两个 fsync 才是"断电不丢"的全部内容。
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
9. **中断也要留全工件**：`AgentLoop.run` 外层 `except BaseException` 只做收尾（`stop_reason=interrupted` + trace + report + **释放租约**）然后**必然重新抛出**；收尾函数自己绝不抛异常。
   启动时接管别人的 run 之前必须先按租约判活——"看到 running 就标 interrupted"在并发下是静默数据损坏。
   接管结论分 `interrupted`（有租约且判死）与 `stale`（没有租约文件），统计里不混算。
10. **超预算就不许发**（admission gate）：`ContextManager.build_result()` 给出 `sendable` / `overflow_reason`
    （`request_too_large` / `prompt_too_large`）。装不下时的顺序是：先 compaction 重算 → 再把当前请求本身卸载成 artifact + 指针 →
    仍超才**不调用 provider**，收敛成 `stop_reason=context_overflow` 的失败运行（one-shot 退出码非 0）。
    **feature flag 只能换策略，不能关掉这道闸**：`context_reduction=off` 时依然生效。
11. **截断必须可逆**：超过 `ARTIFACT_THRESHOLD=4000` 字符的工具输出落进 `.moss/runs/<id>/artifacts/`（按内容 sha12 去重、脱敏后写），
    prompt 里只放压缩摘要 + `read_artifact` 指针。`read_artifact` 的 `path_scope="run_dir"`——用 `Moss.path()` 会放行整个仓库，
    那等于多开一条绕过 `read_file` 的读文件通道。report 里的 `truncated_bytes_lost` 应恒为 0。
12. **副作用要有账**：risky 工具执行前后各落一条 `action_intent` / `action_receipt`（`action_ledger.py`）。
    恢复时"有 intent 无 receipt"的动作，非幂等工具（`run_shell`）**一律不自动重放**——宁可多问一次。
    同一次执行还会把旧内容存进 `.moss/runs/<id>/undo/<action_id>/` 供 `/rewind` 用，
    回滚前必须比对 `after_sha`：用户自己的未提交改动绝不能被悄悄盖掉。
13. **注释风格**：中文、解释"为什么存在/在链路里的位置"，新代码保持一致。
14. **外部能力一律 fail-closed**：MCP 工具没声明 capabilities 就拒绝注册；
    skill 的 `allowed-tools` 越出 run 级白名单就拒绝点亮（不静默取交集——
    静默降级的 skill 会因为缺工具而做错事，报错比降级有用）；
    code mode 没有沙箱就不暴露工具。
15. **稳定前缀不随注册表规模线性膨胀**：Tools 段超过 `catalog_threshold`（默认 16）
    切成目录 + `describe_tool` 按需取 schema，段落有 600 token 预算；
    Skills 段有 400 token 预算。两者都是**按比例缩短描述**而不是砍掉后面的条目——
    被砍掉的东西在模型眼里就是不存在。

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

spec-09 扩展点开关（全部默认关闭或行为不变）：
`--record <dir>` / `--replay <dir>` / `--replay-on-miss=fail|nearest|passthrough`（默认 fail）、
`--aux-model` / `--aux-provider`（不设即全部回落主模型）、
`--enable-code-mode`（**沙箱不可用时即使开了也不暴露 `run_orchestration`**，并打 stderr）。
MCP server 与 `catalog_threshold` 写在 `.moss/config.json` 的 `mcp` 段（结构化配置，不进 `.env`）；
用户钩子放在 `.moss/hooks/<point>`（需要可执行位）。
子命令 `moss mcp serve` 把 moss 的只读工具暴露成 MCP server。

提示词开关：`--tool-protocol=auto|native|text`（默认 auto）、`--context-mode=rerender|append_only`（默认 rerender）、`--no-prompt-cache`。内置 prompt 版本是 `p1`；`.moss/prompts/system.md` 可覆盖稳定 system head，版本记为 `file:<sha256前12位>` 并写入 report/run_manifest。

上下文开关：`--compaction=off|rule|model`（默认 off = 今天的纯截断行为，也是消融基线）、`MOSS_CONTEXT_RATIO`（默认 0.5）、`MOSS_CONTEXT_HARD_CAP`（默认 60000）。

恢复开关：`--resume <id>|latest`、`--resume-parts=memory,plan,history,checkpoint`（默认全部）、
`--explain`（打印恢复会带回什么 + 会不会重放有副作用的动作，然后退出）、`--fork <checkpoint_id>`（从任意
checkpoint 分叉出新会话，原会话不动；undo 不跟着走）。REPL 里 `/rewind [n]` 回退，`/rewind!` 强制。

run 工件保留：`MOSS_RUN_RETENTION_COUNT`（默认 200）/ `MOSS_RUN_RETENTION_DAYS`（默认 30），
两个维度是"或"，设 0 关掉那一维。子命令 `moss runs list|show|verify|prune|pin|export [--otel]`；
`verify` 发现哈希链被改过时返回非零。

CLI 默认：`--max-steps 25`、`--max-new-tokens 4096`、`--approval ask`。ContextManager 预算以**估算 token** 为单位（见 `token_budget.py`，CJK 约 1 char/token、拉丁约 4 chars/token；测试可注入 `measure=len` 走字符级以稳定断言）。
总预算**按模型窗口推导**：`min(context_window * ratio, hard_cap) - max(max_new_tokens, 1024)`；
`ModelCapabilities.known` 为假（认不出来的模型）时退回历史值 12000——拿猜出来的窗口去放大预算，撞的是 provider 的 context-length 报错。
各段按占比切（prefix 35% / history 45% / memory 8% / relevant 7% / constraints 5%），编辑期把 5% 从 history 挪给 relevant+constraints。

## 本地状态目录（全部 gitignored）

```
.moss/sessions/<id>/  用户会话 v2（--resume latest 按 meta.json 的 mtime 取最新）
                      meta.json / history.jsonl / checkpoints.jsonl —— 增量写，不再整份重写
                      旧的单文件 <id>.json 首次 load 时自动迁移，原件留成 <id>.json.v1bak
.moss/cache/       repo map、token 校准等派生缓存（可随时删，会自动重建）
.moss/delegates/   delegate 子 agent 的一次性会话（隔离，防污染 latest）
.moss/runs/index.jsonl  run 索引（append-only，读时按 run_id 折叠）；启动只读它
.moss/runs/<id>.jsonl.gz  过期 run 的归档（逐文件 jsonl，解开仍可 grep）
.moss/runs/<id>/   单次运行审计工件：task_state.json / trace.jsonl / report.json / lease.json
                   artifacts/ 卸载的大工具输出（read_artifact 按行取回）
                   context/   被 compaction 压掉的原始历史 turns-N.jsonl
                   undo/<action_id>/  risky 动作前的旧内容 + manifest.json（/rewind 用）
.moss/skills/      技能 markdown（frontmatter: name/description/allowed-tools/scope/resources/source）
.moss/hooks/       用户钩子 pre_tool/post_tool/pre_final/post_run（需可执行位；agent 写不进来）
.moss/cache/skill_trust.json  第三方 skill 的内容指纹台账（改动后首次使用要确认）
.env               本地密钥（仓库只保留 .env.example）
```
