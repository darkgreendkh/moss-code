# 整体架构

moss 的定位是**一个包在模型外面的控制循环**。模型负责"下一步做什么"，
moss 负责其余全部：把仓库状态整理成 prompt、解析模型的输出、校验并执行动作、
把过程写成可审计的工件、维护跨轮的记忆。

一条贯穿全篇的原则：**模型的输出是"申请"，不是"命令"**。
从解析到落盘的每一环都假设模型可能出错、可能被工具输出里的内容带偏，
因此每一步都要能拒绝、能留痕、能回退。

---

## 1. 分层

```
┌─ 装配层 ────────────────────────────────────────────────┐
│ cli.py            解析参数、加载 .env、构建 provider client、    │
│                   拼出 Moss 实例、跑 REPL 或 one-shot、渲染进度  │
└──────────────────────────┬─────────────────────────────┘
                           │
┌─ 状态层 ──────────────────▼─────────────────────────────┐
│ runtime.py::Moss   facade。所有跨轮状态与全部护栏挂在这里：       │
│                    工具注册表 / 策略 / 记忆 / 会话 / run_store / │
│                    沙箱计划 / 路由 / 钩子 / 预算                │
│                    对外唯一执行入口：run_tool / execute()       │
└──────────────────────────┬─────────────────────────────┘
                           │
┌─ 控制层 ──────────────────▼─────────────────────────────┐
│ agent_loop.py::AgentLoop.run                            │
│   感知 → 决策 → 行动 → 记录，直到停机                        │
└──────────────────────────┬─────────────────────────────┘
                           │
┌─ 能力层 ──────────────────▼─────────────────────────────┐
│ context_manager  prompt_prefix  providers/  output_parser│
│ tool_executor    tools          policy      sandbox      │
│ features/memory  compaction     run_store   checkpoint   │
└──────────────────────────┬─────────────────────────────┘
                           │
┌─ 基础设施 ─────────────────▼─────────────────────────────┐
│ atomic_io  security  clock  token_budget  trace_events   │
│ ignore     lease     run_index                           │
└─────────────────────────────────────────────────────────┘
```

装配层与状态层的分工是刻意的：`cli.py` 决定"这次运行用什么配置"，
`Moss` 决定"运行期能做什么"。所有开关在构建 `Moss` 的那一刻就冻结——
一次 run 内工具集、策略、prompt 前缀都不再变化，否则 prompt cache 每轮都会失效
（见 [features/prompt-context.md](features/prompt-context.md)）。

---

## 2. 一次 `ask()` 的数据流

```
cli.py (装配 / REPL / 进度渲染)
  └─ runtime.py::Moss
       └─ agent_loop.py::AgentLoop.run
            ├─ context_manager.py   每轮按预算组 prompt（六段）+ admission gate
            ├─ providers/clients.py 统一 complete()（Ollama / OpenAI / Anthropic）
            ├─ output_parser.py     模型输出 → ("tool"|"final"|"retry", payload)
            ├─ tool_executor.py     执行护栏（下节展开）
            │    └─ tools.py        工具白名单（显式注册，非动态发现）
            ├─ compaction.py        上下文压缩：结构化交接
            ├─ model_router.py      脏活走 aux model，主线走主模型
            ├─ hooks.py             pre_tool / post_tool / pre_final / post_run
            ├─ checkpoint.py        每步落 checkpoint
            ├─ action_ledger.py     risky 动作的 intent/receipt 两阶段
            └─ run_store.py         .moss/runs/<id>/{task_state,trace,report,lease}
```

每一轮（"步"）都是完整的四拍：

1. **感知** —— `ContextManager` 重新组一份 prompt。稳定前缀（身份/规则/工具/技能/工作区）
   + 历史 + 记忆 + 相关笔记 + 本轮约束 + 当前请求。超预算时先压缩、再卸载、仍超就**不发**。
2. **决策** —— 调 provider。`output_parser` 把回复解析成一批动作或一个最终答案。
   一轮可以有多个 `<tool>` 块；`final` 之后的一律丢弃。
3. **行动** —— 每个动作逐条过 `ToolExecutor`。只读批次可并发，risky 恒串行。
4. **记录** —— 结果写回 history / task_state / trace / memory / checkpoint。
   **写回顺序恒等于 `Action.index`，不是完成顺序**——录制回放依赖这条。

停机条件：模型给出 final（且通过收尾自检）、步数/token/时间/金额任一预算耗尽、
上下文装不下、模型后端报错、用户中断。**每一条路径都必须留下完整工件**。

详见 [features/agent-loop.md](features/agent-loop.md)。

---

## 3. 执行护栏（工具调用的必经之路）

`ToolExecutor.execute` 是所有副作用的唯一闸门，顺序不可调换：

```
allowlist（run 级白名单 + skill 的 allowed-tools 临时覆盖）
  → 存在性
  → 参数校验（ToolSpec.fields）
  → 能力/路径策略（policy.py，risky 但未声明能力 = 拒绝）
  → shell 分级（shell_policy.py，denied 连审批都不给）
  → 重复检测
  → pre_tool 钩子（唯一能改控制流的钩子，退出码 2 = 拒绝）
  → 审批（ask/auto/never，只展示摘要不 dump 完整参数）
  → 前置条件校验（ApprovalReceipt + expected_sha，挡 TOCTOU）
  → action_intent 落盘 + undo 备份
  → 执行（沙箱包裹）
  → 快照 diff + git 事实失效
  → 注入扫描（命中只收紧策略，不拒绝执行）
  → 大输出卸载成 artifact + 压缩摘要
  → action_receipt 落盘 + post_tool 钩子
```

这条链的形状回答了三个问题：

- **能不能做**（allowlist / 校验 / 策略 / 分级）
- **要不要问**（审批 / 前置条件）
- **做完之后账怎么记**（intent / receipt / undo / 快照 / trace）

详见 [features/tool-safety.md](features/tool-safety.md)。

---

## 4. 模块地图

### 主链路

| 模块 | 角色 |
| --- | --- |
| `cli.py` | 装配、REPL、one-shot、进度渲染、`moss runs|memory|mcp` 子命令 |
| `runtime.py` | `Moss` facade。跨轮状态 + 全部护栏的持有者 |
| `agent_loop.py` | 主循环与全部停机路径 |
| `context_manager.py` | 每轮 prompt 组装、分段预算、admission gate |
| `prompt_prefix.py` | 稳定前缀构建、工具目录模式、缓存键 |
| `model_request.py` | 结构化 `system blocks + messages`；仓库内容永不进 system |
| `providers/clients.py` | 三种协议的统一 `complete()` |
| `providers/capabilities.py` | 按 provider/model 显式声明 cache/native/context 能力 |
| `output_parser.py` | 模型输出 → 动作或最终答案（纯函数） |
| `tool_executor.py` | 执行护栏 |
| `tools.py` | 工具白名单与实现 |

### 仓库上下文

| 模块 | 角色 |
| --- | --- |
| `workspace.py` | git 事实 + 分层项目文档 + 文件级快照/diff + 指纹 |
| `repo_map.py` | 目录骨架 + 符号索引 + `rank_relevant_files` 起点锚 |
| `ignore.py` | 手写 `.gitignore` 匹配器。**只用来少扫/少展示，安全判定不依赖它** |
| `retrieval.py` | BM25 + 字段权重 + 时间衰减的召回打分 |

### 安全

| 模块 | 角色 |
| --- | --- |
| `policy.py` | 能力标签 + 路径 glob 作用域，fail-closed |
| `shell_policy.py` | 基于 shlex 的结构化 shell 风险分级（六档） |
| `sandbox.py` | L1 策略 / L2 `sandbox-exec`·`bwrap` / L3 容器，降级必须可见 |
| `injection.py` | 工具输出里的 prompt injection 检测 |
| `security.py` | secret 检测/脱敏、`run_shell` 的环境变量白名单 |
| `hooks.py` | 用户钩子扩展点 |

### 上下文治理

| 模块 | 角色 |
| --- | --- |
| `token_budget.py` | token 估算、在线校准、全部文本裁剪 |
| `compaction.py` | 历史压成结构化交接（可逆/幂等/闭合/因果单元不可拆） |
| `output_compressors.py` | 按输出**形状**注册的压缩器 + generic 兜底 |
| `features/memory.py` | 分层记忆与写入/提炼策略 |
| `features/memory_store.py` | `records.jsonl` 事实源、冲突消解、紧凑化 |

### 持久化与恢复

| 模块 | 角色 |
| --- | --- |
| `atomic_io.py` | 原子 + 持久落盘（临时文件 → fsync → replace → fsync 目录） |
| `run_store.py` | run 目录的全部读写 |
| `run_index.py` | append-only run 索引 + 保留策略 |
| `lease.py` | run 租约（PID + host + boot_id + 心跳 + TTL） |
| `session_store.py` | session v2 目录、增量写、v1 自动迁移 |
| `checkpoint.py` | 每步 checkpoint 与恢复部件 |
| `action_ledger.py` | intent/receipt 两阶段 + 崩溃后对账 |
| `rewind.py` | `/rewind`：文件 + history + memory 一起回退 |
| `task_state.py` | 一次 run 的状态机与停机原因 |

### 观测

| 模块 | 角色 |
| --- | --- |
| `trace_events.py` | 事件名常量 + schema 版本 + `ALL_EVENTS`（**别处禁止写字面量**） |
| `trace_html.py` | `moss runs show --html`，单文件零外部请求 |
| `otel.py` | `moss runs export --otel`，stdlib 生成 OTLP/JSON |
| `budget.py` | 多维预算（步/token/时间/金额） |
| `stall.py` | 四类停滞检测 |
| `verification.py` | "这次 `run_shell` 算不算跑过验证" |

### 扩展点

| 模块 | 角色 |
| --- | --- |
| `delegation.py` | 子 agent 契约：结构化背景进、带证据锚点的结论出 |
| `mcp/` | MCP 客户端与服务端，JSON-RPC over stdio 手写 |
| `skills.py` | `.moss/skills/*.md` 三级渐进披露 + 供应链校验 |
| `model_router.py` | 多模型路由，aux 失败自动回落主模型 |
| `providers/recording.py` | 确定性录制回放（装饰器，对主循环透明） |
| `code_mode.py` | 受限 Python 编排，AST 三层白名单 + 沙箱硬前置 |

### 不在运行时路径上

`evaluation/` 是评测与消融，`benchmarks/` 是任务集与磁带，`scripts/` 是评测入口。
公共 API 只从 `moss/__init__.py` 导出。

---

## 5. 全局不变量

这些性质跨模块生效，改任何一处都要先确认没有把它们破坏掉。
完整清单（含"为什么"）在仓库根的 `CLAUDE.md`，这里只列骨架：

| # | 不变量 |
| --- | --- |
| 1 | 持久化一律走 `atomic_io.write_atomic`，两个 fsync 缺一不可 |
| 2 | 最终答案走 stdout，进度/警告/错误走 stderr；progress observer 异常必须被吞 |
| 3 | 一轮可多动作；写回顺序恒等于 `Action.index` |
| 4 | 错误收敛不裸抛；one-shot 失败必须非零退出 |
| 5 | 所有落盘/展示的文本先过脱敏 |
| 6 | 文件类工具经 `Moss.path()` 锚定在 workspace root 之下；唯一执行入口是 `run_tool`/`execute` |
| 7 | 工具是显式白名单；risky 走审批；审批只展示摘要 |
| 8 | 快照 diff 用 `(mtime_ns, size)`，不做内容 hash |
| 9 | 中断也要留全工件，收尾后**必然重新抛出** |
| 10 | 超预算就不许发（admission gate），feature flag 关不掉这道闸 |
| 11 | 截断必须可逆（artifact + `read_artifact` 指针） |
| 12 | 副作用要有账（intent/receipt/undo） |
| 13 | 注释中文，解释"为什么存在" |
| 14 | 外部能力一律 fail-closed，不静默降级 |
| 15 | 稳定前缀不随注册表规模线性膨胀 |

---

## 6. 数据流的三条主线

理解 moss 最快的方式是分别追这三条线：

**上下文怎么进 prompt**
`workspace.snapshot` / `repo_map` → `prompt_prefix` → `context_manager` 分段 → `model_request` →
provider。看 [features/repo-context.md](features/repo-context.md) 与
[features/prompt-context.md](features/prompt-context.md)。

**动作怎么落地**
`output_parser` → `AgentLoop._execute_tool_batch` → `ToolExecutor.execute` → `tools.py` →
快照 diff → artifact 卸载 → history。看 [features/tool-safety.md](features/tool-safety.md)。

**状态怎么活过重启**
`task_state` + `trace.jsonl` + `checkpoint` + `records.jsonl` + `lease` + `action_ledger` →
`--resume` / `--fork` / `/rewind`。看 [features/sessions-and-runs.md](features/sessions-and-runs.md)。
