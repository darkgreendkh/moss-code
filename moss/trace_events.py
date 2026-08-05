"""trace 事件名常量。

为什么存在：事件名是 trace/report 的对外契约，评测脚本和排查工具都按名字匹配。
散在各处用字面量写，改一处漏一处的成本很高，所以集中在这里。
"""

# 就近文档注入（spec-01 §4.2）
INSTRUCTION_LOADED = "instruction_loaded"
INSTRUCTION_CONFLICT = "instruction_conflict"

# 仓库地图（spec-01 §4.1）
REPO_MAP_BUILT = "repo_map_built"
# 模型第一次命中的文件不在地图给出的候选里——地图把它带偏了。
ANCHOR_MISS = "anchor_miss"

__all__ = [
    "INSTRUCTION_LOADED",
    "INSTRUCTION_CONFLICT",
    "REPO_MAP_BUILT",
    "ANCHOR_MISS",
]
