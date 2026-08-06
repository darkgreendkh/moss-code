"""trace 事件名常量。

为什么存在：事件名是 trace/report 的对外契约，评测脚本和排查工具都按名字匹配。
散在各处用字面量写，改一处漏一处的成本很高，所以集中在这里。
"""

# 主循环骨架（spec-07 §4.5）
RUN_FINISHED = "run_finished"
CHECKPOINT_CREATED = "checkpoint_created"

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

__all__ = [
    "RUN_FINISHED",
    "CHECKPOINT_CREATED",
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
]
