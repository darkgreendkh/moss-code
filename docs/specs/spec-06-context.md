# Spec 06 — 上下文压缩与输出管理

| 项 | 值 |
| --- | --- |
| 状态 | Draft |
| 对应优化章节 | [第 6 章](../plans/archive/2026-agent-upgrade-plan.md)（6.1–6.8） |
| 优先级 | 6.8 是 P0；其余 P1 |
| 依赖 | [spec-04](spec-04-prompt-cache.md)（append-only 布局、usage 真值）、[spec-07](spec-07-session-artifacts.md)（run 目录、trace 事件） |
| 被依赖 | [spec-02](spec-02-agent-loop.md)（软预算触发 compaction）、[spec-08](spec-08-evaluation.md) §4.8（上下文消融） |

## 1. 背景与问题

五段预算（prefix 3000 / memory 1000 / relevant 800 / history 6000，总 12000 token）、按固定顺序反复削、工具输出硬 clip 到 16000 字符。这套机制的本质是**有损截断**：砍掉的内容既不进摘要也不落盘，模型无从取回；20 步之后，前 14 步在 prompt 里是一堆被砍到 20 字符的碎片——既占 token 又没信息。

外加三个具体缺陷：当前请求超预算时只标 flag 却照发；`relevant_memory` 预算为 0 时反而不限量；总预算 12000 写死，而 2026 的窗口普遍 200K+（等于主动只用 6%）。

## 2. 目标 / 非目标

**目标**

1. 引入真正的 compaction：结构化、可逆、幂等、带 `covered_range`。
2. 大输出卸载到 artifact，prompt 里只放指针；新增 `read_artifact` 工具。
3. 按工具类型注册压缩器，保住"失败原因"这类高价值信号。
4. token 估算按 provider 在线自校准。
5. 上下文健康度指标 + 主动干预。
6. 段落顺序调整（硬约束靠近末尾）。
7. 预算按模型窗口推导，扣除输出 reserve。
8. 硬 admission gate：超预算就不许发。

**非目标**

- 不接 provider 原生 compaction API（本地可检查的 handoff 更重要，且不绑定 provider）。
- 不做精确 tokenizer（校准 + 保守估算足够）。
- 不改"当前请求永不裁剪"的既有原则——超预算改为拒发，而不是裁剪它。

## 3. 现状（代码事实）

| 事实 | 位置 |
| --- | --- |
| `SECTION_ORDER = (prefix, memory, relevant_memory, history, current_request)`，总预算 12000 | [moss/context/manager.py:21](moss/context/manager.py#L21)、[moss/context/manager.py:36](moss/context/manager.py#L36) |
| checkpoint 文本拼在 **prefix 尾部**（prompt 最前面） | [moss/context/manager.py:147](moss/context/manager.py#L147) |
| 当前请求超预算只标 `over_budget_unrecoverable` 后照发 | [moss/context/manager.py:213](moss/context/manager.py#L213) |
| `if budget <= 0 or self.measure(candidate) <= budget` → 0 预算不限量 | [moss/context/manager.py:311](moss/context/manager.py#L311) |
| 历史每轮重渲染：最近 6 条各 900 字符，更早折叠 | [moss/context/manager.py:419](moss/context/manager.py#L419) |
| shell 老历史只留 3 行信号行 | [moss/context/manager.py:481](moss/context/manager.py#L481) |
| 工具输出 `clip(..., MAX_TOOL_OUTPUT=16000)`，超出永久丢弃 | [moss/execution/executor.py:232](moss/execution/executor.py#L232) |
| 截断策略只有 head/middle/tail | [moss/execution/executor.py:16](moss/execution/executor.py#L16) |
| `estimate_tokens`：CJK 1:1，拉丁 4:1 | [moss/context/token_budget.py:41](moss/context/token_budget.py#L41) |
| `clip()` 先切 limit 再拼说明，会略超上限（注释已声明为有意取舍） | [moss/context/token_budget.py:112](moss/context/token_budget.py#L112) |
| `clip_to_budget` 二分裁剪，保证 ≤limit | [moss/context/token_budget.py:80](moss/context/token_budget.py#L80) |

## 4. 设计

### 4.1 Compaction

```python
# moss/context/compaction.py
@dataclass(frozen=True)
class CompactionArtifact:
    schema_version: int          # COMPACTION_SCHEMA_VERSION = 1
    id: str
    run_id: str
    covered_seq_start: int       # 被压缩的 trace 事件区间（闭合可查）
    covered_seq_end: int
    method: str                  # rule | model
    created_at: str
    goals: tuple[str, ...]
    completed: tuple[str, ...]
    excluded: tuple[str, ...]         # 已排除的方案（防止重复尝试）
    findings: tuple[Finding, ...]     # 关键发现，必须带证据锚点
    open_questions: tuple[str, ...]
    plan: tuple[dict, ...]
    raw_path: str                # .moss/runs/<id>/context/turns-<n>.jsonl
    before_tokens: int
    after_tokens: int

@dataclass(frozen=True)
class Finding:
    text: str
    evidence: str    # "path:line" 或 "event:<seq>"

def compact(history, trace_events, *, method="rule", budget, aux_client=None
            ) -> tuple[CompactionArtifact, list]: ...
def render_compaction(artifact: CompactionArtifact, budget: int) -> str: ...
```

**触发**：`context_utilization > 0.8`，或 history 段连续 2 轮触发 reduction，或 [spec-02](spec-02-agent-loop.md) 的软预算命中。

**规则模式**（默认，零成本）从 trace 聚合：
- `completed` ← 成功的 `tool_executed`（写类）按 `affected_paths` 归并；
- `excluded` ← `approval_denied` / `capability_denied` / 连续失败后放弃的路径；
- `findings` ← `read_file` 摘要（复用 [spec-05](spec-05-memory.md) 的 `summarize_read_result`）+ 失败的关键错误行，证据锚点直接取 `path:line` 或事件序号；
- `open_questions` ← `stall_detected` 与未完成的 plan 步骤。

**模型模式**：aux model 一次调用，输入是规则模式的产物 + 原始历史指针，输出同一 schema（用 JSON 强约束，解析失败则退回规则模式）。

**因果单元不可拆**：压缩的最小单位是 `ExecutionStep = (tool call, result, 以及它触发的审批/错误/恢复)`。实现上按 `Action.index` + `call_id` 分组；**不允许出现有调用无结果**，也不允许把 `partial_success` 摘要成成功。有测试守。

**可逆**：被压缩的原始 message 写 `.moss/runs/<id>/context/turns-<n>.jsonl`（原子写 + 脱敏），摘要里附路径与行数，模型可用 `read_artifact` 取回。

**幂等**：同一输入 + 同一 method + 同一 schema 版本 → 产出的 artifact（除 `id`/`created_at`）逐字段一致；对已压缩过的区间再压不产生新 artifact。

### 4.2 输出卸载与 `read_artifact`

```python
ARTIFACT_THRESHOLD = 4000     # 字符；超过就落盘
# .moss/runs/<run_id>/artifacts/<seq>-<tool>-<sha12>.txt
```

进 prompt 的形式：

```
<tool_result untrusted="true" source="run_shell" artifact="artifacts/07-run_shell-a1b2c3d4e5f6.txt" lines="1842">
exit_code: 1
[压缩器产出的摘要，见 §4.3]
… 完整输出 1842 行，用 read_artifact("artifacts/07-run_shell-a1b2c3d4e5f6.txt", start, end) 取回
</tool_result>
```

```python
READ_ARTIFACT_TOOL_SPEC = ToolSpec(
    name="read_artifact",
    fields={"path": ToolField(type="str", required=True),
            "start": ToolField(type="int", default=1, minimum=1),
            "end": ToolField(type="int", default=200, minimum=1)},
    risky=False, capabilities=frozenset({"fs_read"}), path_scope="run_dir",
)
```

- 路径锚定在 `.moss/runs/<current_run>/`（`path_scope="run_dir"`，走 [spec-03](spec-03-tool-safety.md) 的 policy），逃逸即拒。
- 内容相同的 artifact 按 sha12 去重（同一 pytest 输出读三次只占一份盘）。
- `truncated_bytes_lost` 指标：卸载后应恒为 0。

### 4.3 按类型压缩

```python
# moss/context/compressors.py
def detect_kind(tool_name: str, args: dict, text: str) -> str: ...
def compress(kind: str, text: str, budget: int) -> tuple[str, dict]: ...
def register(kind: str, fn): ...
```

| kind | 检测 | 保留策略 |
| --- | --- | --- |
| `pytest` | 输出含 `= FAILURES =` / `passed` / `failed` 汇总行 | 每个失败的用例名 + assert 行 + traceback 末 15 行 + 汇总行 |
| `ruff`/`mypy` | `path:line:col: CODE msg` 形状 | 按 `CODE` 聚合折叠计数，每类保留前 3 条 |
| `search_text` | 工具名 | 每文件最多 k 条命中 + 文件级计数汇总 |
| `git_diff` | 输出以 `diff --git` 开头 | 按 hunk 保留；超预算保留 hunk 头 + `+N/-M` 统计 |
| `list_files` | 工具名 | 按扩展名聚合计数 + 列目录 |
| `generic` | 兜底 | 现有 head/middle/tail |

**不可丢失清单**（任何压缩器都必须保留）：`exit_code`、状态（success/partial_success/error）、`affected_paths`、artifact 指针。有测试守。

### 4.4 token 在线校准

```python
# moss/context/token_budget.py
@dataclass
class Calibration:
    provider: str
    model: str
    ratio: float          # actual / estimated 的滑动中位数
    samples: int
    updated_at: str

def calibrated_measure(base_measure, calibration) -> Callable[[str], int]: ...
```

- 每轮把 `(estimated_prompt_tokens, usage.input_tokens)` 追加到 `.moss/cache/token_calibration.json`（保留最近 50 条，按 `(provider, model)` 分桶）。
- 下一轮预算用 `estimate_tokens(text) * ratio`；`samples < 5` 时 ratio 固定为 1.0（不乱调）。
- 偏差 >30% 时 report 里告警 `token_estimate_drift`。
- 可选：探测到 `tiktoken` 一类库（import 探测，**不写进依赖**）时直接用真值，`ratio` 强制 1.0。

### 4.5 段落顺序与健康度

新顺序：

```
prefix(身份/规则/工具/skills)        ← 稳定，进缓存段
workspace + repo_map                  ← run 内冻结
history（append-only）
memory + relevant_memory
constraints（checkpoint / plan / 最近失败 / 硬约束）   ← 从 prefix 尾部搬到这里
current_request                       ← 永远最后
```

各段加显式用途说明（"以下是历史，供参考"/"以下是当前必须遵守的约束"）。`SECTION_ORDER` 与 `DEFAULT_REDUCTION_ORDER` 同步更新；`constraints` 是新段，预算从 memory 段划 300。

**健康度指标**（每轮进 `prompt_built` 的 metadata 与 trace）：

| 指标 | 定义 |
| --- | --- |
| `context_utilization` | `measure(prompt) / model_context_window`（不是 /12000） |
| `section_share` | 各段 token 占比 |
| `distractor_ratio` | BM25 相关度低于阈值的 token 占比（复用 [spec-05](spec-05-memory.md) 的索引） |
| `history_staleness` | 最老一条 history 距今多少步 |

超阈值 → 触发 compaction 或建议 sub-agent 隔离（[spec-09](spec-09-new-modules.md) §4.1）。

### 4.6 预算按窗口推导

```python
total_budget = min(caps.context_window * ratio, hard_cap) - output_reserve
# ratio 默认 0.5（MOSS_CONTEXT_RATIO）
# output_reserve = max(max_new_tokens, 1024)
# hard_cap 默认 60000（MOSS_CONTEXT_HARD_CAP），防止一次请求过大
```

- `caps.context_window` 来自 [spec-04](spec-04-prompt-cache.md) 的能力表；未知则退回今天的 12000（小窗口模型行为不变）。
- 各段按比例分配（prefix 25% / workspace 10% / history 45% / memory 8% / relevant 7% / constraints 5%），并按任务阶段微调（探索期 history 多，编辑期 relevant/constraints 多）。
- 段落下限 `floor = budget // 4`（最小 20）保持现有逻辑不变。

### 4.7 硬 admission gate

```python
@dataclass
class ContextBuildResult:
    request: ModelRequest
    text: str
    metadata: dict
    sendable: bool
    overflow_reason: str | None    # request_too_large | prompt_too_large
```

不可发送时的处理顺序：

1. 触发 compaction（§4.1）后重算；
2. 仍超 → 把当前请求本身卸载成 artifact，prompt 里放摘要 + 指针，提示模型分段读；
3. 仍超 → **不调用 provider**，`stop_reason=context_overflow`，stderr 打印可读原因（"当前请求约 N token，超过模型窗口 M 的可用预算"），one-shot 模式退出码非 0。

同时修掉 `relevant_memory` 的 0 预算逻辑：`budget <= 0` → 该段渲染为空。

**feature flag 只能换策略，不能关掉这道闸**（`context_reduction=off` 时依然要有 admission gate）。

### 4.8 涉及文件

| 文件 | 改动 |
| --- | --- |
| `moss/context/compaction.py` | 新增 |
| `moss/context/compressors.py` | 新增 |
| [moss/context/manager.py](moss/context/manager.py) | 段落顺序 + `constraints` 段；健康度指标；admission gate；0 预算修正；预算按窗口推导 |
| [moss/context/token_budget.py](moss/context/token_budget.py) | 校准；`clip_to_budget` 用于所有进 prompt 的文本 |
| [moss/execution/executor.py](moss/execution/executor.py) | 卸载 artifact；调用 `output_compressors` |
| [moss/execution/registry.py](moss/execution/registry.py) | `read_artifact` 工具 |
| [moss/runs/store.py](moss/runs/store.py) | `artifacts/` 与 `context/` 子目录的路径方法 |
| [moss/agent/loop.py](moss/agent/loop.py) | compaction 触发点；`sendable=False` 的收尾 |

## 5. 兼容与迁移

- `ContextManager.build` 继续返回 `(prompt, metadata)`；`ContextBuildResult` 通过 `build_result()` 新方法暴露。
- metadata 里现有的 `*_chars` 字段名保留（评测代码在用），**新增**同名 `*_tokens` 字段并在报告里改用后者；一个版本周期后废弃旧字段。
- 段落顺序变化会改变 prompt 文本 → 现有断言 prompt 结构的测试需要更新（预期内，改测试而不是改设计）。
- `--context-mode=truncate_only` 保留今天的行为作为消融基线，**默认值先保持 `truncate_only`**，等 [spec-08](spec-08-evaluation.md) 的配对检验证明收益后再翻成 `compaction`。
- `read_artifact` 是新工具 → 稳定头变化，与其它稳定头改动合并上线。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| `tests/test_admission_gate.py`（新） | 1 MiB 请求：provider mock 调用次数为 0；退出码非 0；stderr 有可读原因 |
| 同上 | `relevant_memory` 预算为 0 → 该段渲染为空（现在会全渲染） |
| `tests/test_compaction.py`（新） | 幂等（同输入两次产出一致）；`covered_range` 闭合（覆盖 + 保留 + 省略 = 全集）；因果单元不可拆（不存在孤立 call/result）；`partial_success` 不被摘要成 success；原始历史可通过 `read_artifact` 取回 |
| `tests/test_output_compressors.py`（新） | pytest 失败输出：失败用例名 + assert 行必须保留；ruff 聚合；git diff hunk；不可丢失清单全项断言 |
| `tests/test_artifact_offload.py`（新） | 超阈值输出落盘 + 指针格式；`read_artifact` 路径逃逸被拒；相同内容去重；`truncated_bytes_lost=0` |
| `tests/test_token_calibration.py`（新） | 样本 <5 时 ratio=1.0；校准后估算误差收敛；漂移告警触发 |
| `tests/test_context_layout.py`（新） | `constraints` 段在 history 之后、request 之前；各段用途说明存在 |
| `tests/test_moss.py`（扩展） | 大窗口 provider 下预算 >12000；Ollama 小窗口行为不变 |

## 7. 验收标准

| 指标 | 门槛 |
| --- | --- |
| 50 步任务的 prompt token 曲线 | 从"线性增长撞预算"变成锯齿状稳定 |
| compaction 后 3 步内任务成功率 | 不下降（配对检验，[spec-08](spec-08-evaluation.md) §4.4） |
| 信息保留率（关键事实可召回比例） | ≥90%（judge 判定） |
| `truncated_bytes_lost` | 0 |
| 失败原因保留（人工/自动抽检） | ≥95%（`error_signal_lost` 标签占比 <5%） |
| token 估算误差 P90 | <10% |
| provider context-length 报错 | 0 |
| compaction 幂等性 | 100% |

## 8. 实施顺序（PR 拆分）

1. **PR-1（P0，S）**：admission gate + 0 预算修正 + 严格 `clip_to_budget`。**先堵住"超预算照发"这个洞**。
2. **PR-2（P1，M）**：artifact 卸载 + `read_artifact` 工具（与稳定头改动合并）。
3. **PR-3（P1，M）**：`output_compressors` 注册表 + 5 类压缩器。
4. **PR-4（P1，S）**：token 在线校准。
5. **PR-5（P1，S）**：段落顺序 + `constraints` 段 + 健康度指标。
6. **PR-6（P1，S）**：预算按窗口推导（依赖 [spec-04](spec-04-prompt-cache.md) 的能力表）。
7. **PR-7（P1，L）**：`moss/context/compaction.py` 规则版 + 触发点 + 可逆存储。
8. **PR-8（P1，M）**：compaction 模型版（依赖 [spec-09](spec-09-new-modules.md) 的 aux model）。

## 9. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| compaction 把失败摘要成成功 | 结构化 schema（不是自由文本）+ `partial_success` 断言 + 证据锚点必填 + fidelity 评测套件 |
| 压缩器切掉了真正重要的行 | 不可丢失清单 + `error_signal_lost` 标签 + `generic` 兜底 |
| 预算放大到 60000 后成本失控 | 靠 [spec-02](spec-02-agent-loop.md) 的 token/$ 预算兜底；`MOSS_CONTEXT_RATIO` 可调小 |
| 校准把估算带偏 | 样本阈值 + 只在 ±30% 内生效，超出则告警并退回 1.0 |
| 段落顺序改动让现有测试大面积失败 | 预期内；PR-5 单独提交，一次改完 |
| artifact 目录膨胀 | 内容去重 + [spec-07](spec-07-session-artifacts.md) 的 run 保留策略统一清理 |

## 10. 开放问题

1. compaction 之后要不要保留"最近 N 轮不压缩"的窗口？倾向：保留最近 3 个 ExecutionStep 原样，避免刚发生的错误被摘要掉。
2. `distractor_ratio` 的阈值怎么定？倾向：先只观测不干预，收集一轮数据再定阈值。
3. `read_artifact` 是否该允许跨 run 读（调试历史 run）？倾向：模型不允许，CLI 允许（`moss runs show`）。
4. 卸载阈值 4000 字符是否偏低（会产生很多小文件）？倾向：先按 4000 跑，看 artifact 数量分布再调。
