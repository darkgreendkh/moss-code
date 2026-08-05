# Spec 02 — 运行时主循环

| 项 | 值 |
| --- | --- |
| 状态 | Draft |
| 对应优化章节 | [第 2 章](docs/optimize/2026-agent-upgrade-plan.md)（2.1–2.6） |
| 优先级 | 2.6 是 P0；其余 P1 |
| 依赖 | [spec-04](spec-04-prompt-cache.md)（原生协议保留 `call_id`）、[spec-03](spec-03-tool-safety.md)（批内审批）、[spec-07](spec-07-session-artifacts.md)（trace 事件常量、中断工件） |
| 被依赖 | [spec-08](spec-08-evaluation.md)（`model_turns`/`RunBudget` 是成本受控评测的数据源） |

## 1. 背景与问题

主循环是标准的 `while tool_steps < max_steps`：组 prompt → 一次模型调用 → 解析出**一个**动作 → 执行 → 记录。三个后果：

- "读 3 个文件再决定"要 3 轮模型调用、3 份完整 prompt 的输入成本；
- 长任务没有任何显式计划，跑偏了只能靠步数上限兜底；
- 停机条件只有"模型说完了"和"步数/重试上限"——没有 token/时间/成本护栏，没有停滞检测，没有收尾前验证。

外加两个确定性缺陷：`KeyboardInterrupt` 之后 run 目录停在 `running` 且没有 report；任何非空裸文本都会被当成 final。

## 2. 目标 / 非目标

**目标**

1. 一轮可执行多个动作，只读动作有界并行，且结果顺序确定（可回放）。
2. 引入显式计划（`update_plan`）与窗口级停滞检测。
3. 引入多维预算（步数 / token / 时间 / 成本）与优雅收尾。
4. 收尾前对"改了文件但没验证"注入一次自检提示。
5. 中断（含 `SIGTERM`/异常）路径也留下完整工件。

**非目标**

- 不引入显式状态机 / event reducer（见定稿方案 0.4 的拒绝理由）。`task_state.status + stop_reason` 继续作为状态真相。
- 不做跨进程调度、不做可写并行子 agent（前者无场景，后者要等 [spec-03](spec-03-tool-safety.md) 沙箱 + [spec-07](spec-07-session-artifacts.md) 幂等）。
- 不改 `AgentLoop.run(user_message) -> str` 的对外签名。

## 3. 现状（代码事实）

| 事实 | 位置 |
| --- | --- |
| `while tool_steps < max_steps and attempts < max_attempts`，`max_attempts = max(max_steps*3, max_steps+4)` | [moss/agent_loop.py:44](moss/agent_loop.py#L44) |
| 用户消息先进 history，再由 context_manager 作为 current request 渲染 → 首轮出现两次 | [moss/agent_loop.py:20](moss/agent_loop.py#L20)、[moss/context_manager.py:141](moss/context_manager.py#L141) |
| `parse_model_output` 只返回单个 `(kind, payload)` | [moss/output_parser.py:29](moss/output_parser.py#L29) |
| 任何非空裸文本 → final | [moss/output_parser.py:50](moss/output_parser.py#L50) |
| 原生 tool_use 被序列化回 `<tool>` 文本，只取第一个，`call_id` 丢弃 | [moss/providers/clients.py:107](moss/providers/clients.py#L107) |
| `repeated_tool_call` 只比较最近 2 条 tool 事件 | [moss/runtime.py:443](moss/runtime.py#L443) |
| 每步落 checkpoint | [moss/agent_loop.py:191](moss/agent_loop.py#L191) |
| 模型后端异常有专门 finalizer，其它异常没有 | [moss/agent_loop.py:266](moss/agent_loop.py#L266) |
| 预算只有 `max_steps` / `max_new_tokens` | [moss/cli.py:448](moss/cli.py#L448) |

## 4. 设计

### 4.1 动作批（ToolBatch）

```python
# moss/output_parser.py
@dataclass(frozen=True)
class Action:
    kind: str            # "tool" | "final" | "retry"
    name: str | None
    args: dict | None
    text: str | None     # final / retry 的文本
    index: int           # 在本轮输出中的出现次序，决定执行与记录顺序
    call_id: str | None  # 原生协议的调用 ID，回传结果时原样带回

def parse_model_actions(raw, *, protocol="text") -> list[Action]: ...
def parse_model_output(raw):
    """保留旧签名：返回 parse_model_actions 的第一个动作的 (kind, payload)。"""
```

**兼容硬约束**：只有一个 `<tool>` 块时，`parse_model_output` 的返回值必须与现在**逐字节一致**（现有 `tests/test_output_parser.py` 不改一行即通过）。

**执行规则**（`AgentLoop._execute_batch`）：

| 情况 | 行为 |
| --- | --- |
| 批内全是只读工具（`risky=False` 且 `capabilities ⊆ {fs_read}`） | `ThreadPoolExecutor(max_workers=min(4, len(batch)))` 并发 |
| 批内含任一 risky 工具 | 整批降级串行，逐个走审批 |
| 批内含 `final` | 先执行 `final` 之前的工具，`final` 之后的动作丢弃并记 `batch_truncated` |
| 任一动作校验失败 | 该动作记 error，其余照常执行（不整批回滚——工具本来就是独立的） |
| 并发中任一动作抛异常 | 收敛为该动作的 error 结果，不传播出批 |

**顺序不变量**：写回 history / trace 的顺序**按 `Action.index`**，不是完成顺序。这条是 [spec-09](spec-09-new-modules.md) 录制回放能成立的前提，必须有测试守住。

**并发安全**：只读工具不写工作区，但会写 memory（`update_memory_after_tool`）。做法是并发阶段只做纯执行，`update_memory_after_tool` / `record` / `emit_trace` 全部在主线程按 index 串行补做。

### 4.2 步数记账

新增两个字段，旧字段语义不动：

| 字段 | 含义 | 用途 |
| --- | --- | --- |
| `tool_steps` | **不变**：执行成功的工具调用数 | 现有 `step_budget`、`within_budget` 口径继续有效 |
| `model_turns` | 模型调用轮数 | [spec-08](spec-08-evaluation.md) 的主口径 |
| `tool_calls` | 工具调用总数（含失败） | 失败率、无效调用率 |

`max_steps` 继续管 `tool_steps`；并行不会让一个 25 步预算变成 100 次工具调用。

### 4.3 计划状态机

```python
# 新工具，非 risky，capabilities = frozenset()
UPDATE_PLAN_TOOL_SPEC = ToolSpec(
    name="update_plan",
    fields={"steps": ToolField(type="list", required=True)},
    risky=False,
    description="Replace the current plan. Each step: {id, title, status}.",
)
```

- `status ∈ {pending, in_progress, done, blocked}`，最多 20 步，`title` 每条 ≤120 字符（超出截断）。
- 写入 `task_state.plan`（新字段，`to_dict`/`from_dict` 带默认空列表，旧 session 可读）。
- 渲染进 checkpoint 段，位置靠近 prompt 尾部（见 [spec-06](spec-06-context.md) §4.5 的段落顺序）。
- 软预算：某一 `in_progress` 步消耗超过 `max_steps / max(1, len(plan)) * 2` 时注入一次 runtime notice 建议重规划，落 trace `plan_pressure`。
- `plan_drift` 标签（计划声明与实际工具序列不符）由 [spec-08](spec-08-evaluation.md) 的失败分类器离线判定，主循环只负责把 plan 落进 trace。

### 4.4 收尾前自检

```python
def _needs_verification(task_state, trace_flags) -> bool:
    """改了文件却一次验证都没跑过 —— 在收尾前拦一次。"""
    return (trace_flags.workspace_changed
            and not trace_flags.saw_test_command
            and not task_state.verification_requested)
```

- `saw_test_command` 判定：本 run 内存在 `tool_executed{tool="run_shell"}` 且其 `shell_risk_class`（[spec-03](spec-03-tool-safety.md) §4.1 产出）标记为 `test`，或 argv[0] ∈ {`pytest`, `python -m pytest`, `npm test`, `cargo test`, `go test`, `make test`}。
- 命中时不收尾，注入 runtime notice，置 `task_state.verification_requested=True`（**最多一次**），`attempts += 1`，落 trace `verification_requested`。
- 开关：`--verify-before-final=on|off`（默认 on），评测里作为消融维度。

### 4.5 停滞检测

```python
# moss/stall.py
@dataclass(frozen=True)
class StallSignal:
    kind: str      # repeat_exact | ab_loop | no_progress | error_storm
    detail: str
    window: int

def detect_stall(events, *, window=8) -> StallSignal | None: ...
```

| 规则 | 判定 |
| --- | --- |
| `repeat_exact` | 窗口内同一 `(tool, args_digest)` 出现 ≥3 次 |
| `ab_loop` | 窗口内 2-gram 或 3-gram 的 `(tool, args_digest)` 序列重复 ≥2 轮 |
| `no_progress` | 连续 4 步 `workspace_changed=False` 且读到的路径集合无新增 |
| `error_storm` | 同一 `tool_error_code` 连续 3 次 |

命中后注入**结构化**干预（"你在重复 X；已知失败原因是 Y；请换一种方式，或返回 final 说明为什么做不到"），而不是简单拒绝执行。落 trace `stall_detected`。现有 `repeated_tool_call`（[moss/runtime.py:443](moss/runtime.py#L443)）保留为 `repeat_exact` 的一个特例，避免删掉现有测试。

### 4.6 多维预算

```python
# moss/budget.py
@dataclass
class RunBudget:
    max_steps: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_wall_clock_s: float | None = None
    max_usd: float | None = None

    def consume(self, *, input_tokens=0, output_tokens=0, elapsed_s=0.0, usd=0.0): ...
    def soft_exceeded(self) -> str | None: ...   # 达到 80% 的第一个维度
    def hard_exceeded(self) -> str | None: ...   # 超限的第一个维度
    def snapshot(self) -> dict: ...              # 进 report 的 usage 段
```

- 真实 usage 来自 `model_client.last_completion_metadata`（Anthropic 侧的解析见 [spec-04](spec-04-prompt-cache.md) §4.5）；拿不到时用 `estimate_tokens` 兜底并置 `usage_estimated=True`。
- 金额换算查 `moss/evaluation/pricing.py`（[spec-08](spec-08-evaluation.md) §4.6），查不到价格时 `usd=None` 而不是 0。
- **软阈值**：触发一次 compaction（[spec-06](spec-06-context.md)）+ 一条"请开始收敛"的 runtime notice，落 `budget_soft_exceeded`。
- **硬阈值**：不再调用模型，走优雅收尾——写 checkpoint、生成"已完成 / 未完成 / 建议下一步"的结构化 final，`stop_reason=budget_exceeded`，落 `budget_exceeded`。
- CLI：`--max-input-tokens` / `--max-output-tokens` / `--max-seconds` / `--max-usd`，默认全 `None`（行为与今天一致）。

### 4.7 中断与取消

```python
try:
    ...主循环...
except BaseException as exc:            # 含 KeyboardInterrupt / SystemExit
    self._finish_interrupted(task_state, exc)
    raise
```

`_finish_interrupted` 复用 `_finish_model_error` 的收尾骨架：`stop_reason=interrupted`、写 checkpoint、写 trace `run_interrupted`、写 report、释放 run 租约（[spec-07](spec-07-session-artifacts.md) §4.4）。**语义不变**：Ctrl-C 仍然只取消当前轮，异常继续向上抛。

取消令牌：`AgentLoop` 持有 `cancel_token`（`threading.Event`），`run_shell` 用 `start_new_session=True` 起独立进程组，超时/取消时 `killpg` 整组，避免孤儿进程。

### 4.8 新增 trace 事件

`tools_batch_started{count, parallel}` · `tools_batch_finished{count, duration_ms}` · `plan_updated{steps}` · `plan_pressure{step_id}` · `stall_detected{kind, detail}` · `verification_requested` · `budget_soft_exceeded{dimension}` · `budget_exceeded{dimension}` · `run_interrupted{reason}` · `batch_truncated{dropped}`

全部走 `moss/trace_events.py` 常量（[spec-07](spec-07-session-artifacts.md) §4.5）。

### 4.9 涉及文件

| 文件 | 改动 |
| --- | --- |
| [moss/output_parser.py](moss/output_parser.py) | `Action`、`parse_model_actions`；`parse_model_output` 变薄封装 |
| [moss/agent_loop.py](moss/agent_loop.py) | 批执行、计划、自检、停滞、预算、中断收尾 |
| `moss/stall.py` | 新增 |
| `moss/budget.py` | 新增 |
| [moss/tools.py](moss/tools.py) | `UPDATE_PLAN_TOOL_SPEC` + `tool_update_plan` |
| [moss/task_state.py](moss/task_state.py) | 新字段 `plan` / `model_turns` / `tool_calls` / `verification_requested` / `budget`；`STOP_REASON_BUDGET_EXCEEDED`、`STOP_REASON_INTERRUPTED`（若无） |
| [moss/tool_executor.py](moss/tool_executor.py) | 支持按批调用；memory/record/trace 回主线程串行 |
| [moss/cli.py](moss/cli.py) | 新 CLI 参数；`--parallel-tools`、`--verify-before-final` |
| [moss/runtime.py](moss/runtime.py) | `repeated_tool_call` 委托给 `moss/stall.py` |

## 5. 兼容与迁移

- `task_state` 新字段全部有默认值，`from_dict` 对旧 JSON 直接可读；`to_dict` 增加字段不破坏既有断言（现有测试断言的是具体 key 存在，不是 key 集合相等——落地前先跑一遍确认）。
- `--parallel-tools=off`（默认 **off**，先灰度）时行为与今天完全一致；评测证明收益后再翻默认值。
- `update_plan` 是新工具 → `tool_signature` 变化 → prompt 稳定头变化。必须与 [spec-04](spec-04-prompt-cache.md) 的 run 内冻结一起上线，否则缓存键会抖。
- 首轮用户请求重复（bug #20）在本 spec 一并修：`agent.record` 的 user 条目打 `pending=True`，history 渲染跳过 `pending` 条目，模型返回后清除标记。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| `tests/test_output_parser.py`（不改） | 单动作路径逐字节兼容 |
| `tests/test_actions_batch.py`（新） | 多 `<tool>` 块解析顺序；`final` 截断后续；`call_id` 保留 |
| `tests/test_agent_loop_parallel.py`（新） | 3 个只读动作并发执行；history 顺序 = index 顺序（跑 20 次全一致）；含 risky 时降级串行且每个都走审批 |
| `tests/test_stall.py`（新） | 四类停滞各一组样例 + 一组不该触发的负样例 |
| `tests/test_budget.py`（新） | 软/硬阈值触发；`usd=None` 不被当作 0；硬超限时模型 mock 调用次数为 0 |
| `tests/test_interrupt_artifacts.py`（新） | 在第 k 步注入 `KeyboardInterrupt`，断言 task_state/trace/report 三件套齐全且 `stop_reason=interrupted`，异常仍被抛出 |
| `tests/test_moss.py`（扩展） | `update_plan` 写入与渲染；`verification_requested` 最多注入一次；首轮 prompt 里 `Current user request` 只出现一次 |

## 7. 验收标准

| 指标 | 门槛 |
| --- | --- |
| `model_turns`（固定任务集） | 下降 ≥30% |
| 总输入 token | 下降 ≥25% |
| pass_rate | 不下降（[spec-08](spec-08-evaluation.md) §4.4 的配对检验判定） |
| 批执行确定性 | 同一批动作重放 20 次，history 逐字节一致 |
| `unverified_edit_rate` | <10% |
| `no_progress_loop` / `retry_limit_reached` 占比 | 下降（相对基线，报配对区间） |
| 中断工件完整率 | 100% |
| `run_shell` 超时后孤儿进程数 | 0 |

## 8. 实施顺序（PR 拆分）

1. **PR-1（P0，S）**：中断收尾 `_finish_interrupted` + 进程组终止 + 测试。
2. **PR-2（P0，S）**：首轮请求重复修复（`pending` 标记）。
3. **PR-3（P1，S）**：`model_turns` / `tool_calls` 记账字段（先只记录，不改行为，给评测建基线）。
4. **PR-4（P1，M）**：`parse_model_actions` + 批执行（默认串行，`--parallel-tools=off`）。
5. **PR-5（P1，S）**：只读并行开关打开 + 顺序不变量测试。
6. **PR-6（P1，S）**：`moss/stall.py` 接管停滞检测。
7. **PR-7（P1，M）**：`moss/budget.py` + CLI 参数 + report `usage` 段。
8. **PR-8（P1，M）**：`update_plan` 工具 + 计划渲染（与 [spec-04](spec-04-prompt-cache.md) PR 合并上线）。
9. **PR-9（P1，S）**：收尾前自检。

## 9. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| 并行破坏可回放性 | 顺序不变量测试 + 默认关闭 + `--parallel-tools=off` 一键回退 |
| 并行下 memory 写入竞态 | 并发阶段不碰 memory，全部回主线程串行补做 |
| 步数记账口径改变让历史指标不可比 | `tool_steps` 语义完全不动；新口径只加不改，报告里注明切换点 |
| 自检提示引入额外成本 | 最多一次；`--verify-before-final=off` 可关；成本进 8.5 的账 |
| 新增工具让稳定前缀变化 | 与 run 内冻结一起上线；`prompt_cache_key` 恒定有断言测试 |
| `except BaseException` 吞掉不该吞的 | 只做收尾，**必然 `raise`**；测试断言异常仍传播 |

## 10. 开放问题

1. 并行的 `max_workers` 是否该按 provider 延迟自适应？倾向：先固定 4，观察 `tools_batch_finished.duration_ms` 再说。
2. `final` 之后还有工具调用时，除了丢弃是否该反过来判为"模型没想清楚"而触发一次 retry？倾向：先只记 `batch_truncated`，等失败分类学有数据再定。
3. 硬预算触发的"优雅收尾"是否要花一次模型调用来生成总结？倾向：默认用规则生成（免费），`--budget-summary=model` 可选。
