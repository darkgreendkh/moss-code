"""trace 事件名常量与 schema 版本。

为什么存在（spec-07 §4.5）：
事件名是 trace/report 的对外契约，评测脚本和排查工具都按名字匹配。散在各处用
字面量写，改一处漏一处的成本很高——而漏掉的表现不是报错，是某个指标悄悄变成 0，
没人会注意到。集中在这里之后，改事件名会在**导入期**炸掉（`tests/test_trace_events.py`
用 AST 扫 `moss/evaluation/` 里的字符串字面量，命中已知事件名就报错）。
"""

# trace 的 schema 版本。字段含义发生不兼容变化时 +1；
# 评测侧读到旧版本的事件时按旧字段名兼容解析一个版本周期。
TRACE_SCHEMA_VERSION = 2

# 主循环骨架（spec-07 §4.5）
RUN_STARTED = "run_started"
PROMPT_BUILT = "prompt_built"
MODEL_REQUESTED = "model_requested"
MODEL_PARSED = "model_parsed"
MODEL_ERROR = "model_error"
TOOL_EXECUTED = "tool_executed"
CHECKPOINT_CREATED = "checkpoint_created"
RUNTIME_IDENTITY_MISMATCH = "runtime_identity_mismatch"
RUN_FINISHED = "run_finished"

# 就近文档注入（spec-01 §4.2）
INSTRUCTION_LOADED = "instruction_loaded"
INSTRUCTION_CONFLICT = "instruction_conflict"

# 仓库地图（spec-01 §4.1）
REPO_MAP_BUILT = "repo_map_built"
# 模型第一次命中的文件不在地图给出的候选里——地图把它带偏了。
ANCHOR_MISS = "anchor_miss"

# 主循环（spec-02 §4.8）
TOOLS_BATCH_STARTED = "tools_batch_started"
TOOLS_BATCH_FINISHED = "tools_batch_finished"
BATCH_TRUNCATED = "batch_truncated"
PLAN_UPDATED = "plan_updated"
PLAN_PRESSURE = "plan_pressure"
STALL_DETECTED = "stall_detected"
VERIFICATION_REQUESTED = "verification_requested"
BUDGET_SOFT_EXCEEDED = "budget_soft_exceeded"
BUDGET_EXCEEDED = "budget_exceeded"
RUN_INTERRUPTED = "run_interrupted"

# 提示词缓存（spec-04 §4.4）
TOOL_REGISTRY_DRIFT = "tool_registry_drift"
CACHE_CAPABILITY_DETECTED = "cache_capability_detected"

# 结构化记忆（spec-05 §4.9）
MEMORY_POISONING_BLOCKED = "memory_poisoning_blocked"

# 上下文压缩与输出管理（spec-06）
CONTEXT_OVERFLOW = "context_overflow"
CONTEXT_COMPACTED = "context_compacted"
REQUEST_OFFLOADED = "request_offloaded"

# 动作意图/回执（spec-07 §4.6）。两条成对出现：
# 有 intent 无 receipt 的那一段，就是崩溃窗口里"不知道做没做"的动作。
ACTION_INTENT = "action_intent"
ACTION_RECEIPT = "action_receipt"
ACTION_RECONCILED = "action_reconciled"

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "RUN_STARTED",
    "PROMPT_BUILT",
    "MODEL_REQUESTED",
    "MODEL_PARSED",
    "MODEL_ERROR",
    "TOOL_EXECUTED",
    "CHECKPOINT_CREATED",
    "RUNTIME_IDENTITY_MISMATCH",
    "RUN_FINISHED",
    "CONTEXT_OVERFLOW",
    "CONTEXT_COMPACTED",
    "REQUEST_OFFLOADED",
    "TOOLS_BATCH_STARTED",
    "TOOLS_BATCH_FINISHED",
    "BATCH_TRUNCATED",
    "PLAN_UPDATED",
    "PLAN_PRESSURE",
    "STALL_DETECTED",
    "VERIFICATION_REQUESTED",
    "BUDGET_SOFT_EXCEEDED",
    "BUDGET_EXCEEDED",
    "RUN_INTERRUPTED",
    "TOOL_REGISTRY_DRIFT",
    "CACHE_CAPABILITY_DETECTED",
    "MEMORY_POISONING_BLOCKED",
    "INSTRUCTION_LOADED",
    "INSTRUCTION_CONFLICT",
    "REPO_MAP_BUILT",
    "ANCHOR_MISS",
    "ACTION_INTENT",
    "ACTION_RECEIPT",
    "ACTION_RECONCILED",
    "ALL_EVENTS",
]

# 所有合法事件名。trace 里出现的事件名必须在这个集合内（有测试守着）——
# 一个不在集合里的名字要么是打错了，要么是加了常量却忘了登记。
ALL_EVENTS = frozenset(
    value
    for name, value in list(globals().items())
    if name.isupper() and name not in ("TRACE_SCHEMA_VERSION", "ALL_EVENTS") and isinstance(value, str)
)
