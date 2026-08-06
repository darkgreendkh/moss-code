"""评测层级与证据边界。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LevelDefinition:
    code: str
    name: str
    can_prove: str
    cannot_prove: str
    frequency: str


LEVELS = {
    "L0": LevelDefinition("L0", "不变量", "单元正确性与安全不变量", "端到端行为", "每次提交"),
    "L1": LevelDefinition(
        "L1",
        "合同",
        "给定模型动作时 harness 的执行、护栏、工件和恢复确定",
        "模型能力与任务难度",
        "每次提交",
    ),
    "L2": LevelDefinition(
        "L2",
        "能力",
        "当前 harness 与模型在真实任务上的成功率和成本",
        "泛化到其他仓库或模型",
        "每周或发版前",
    ),
    "L3": LevelDefinition("L3", "对抗", "已知攻击下护栏是否成立", "未知攻击", "每周"),
    "L4": LevelDefinition(
        "L4",
        "成本-效用",
        "已测改动的收益、成本与失败集中位置",
        "未运行配置的收益",
        "每次 L2/L3 后",
    ),
}


def classify_artifact(artifact):
    """把历史 v2 工件降级为 L1；v3 必须自己声明层级。"""
    artifact = dict(artifact or {})
    schema_version = int(artifact.get("schema_version", 0) or 0)
    legacy = schema_version == 2
    if legacy:
        artifact.setdefault("eval_level", "L1")
        artifact.setdefault("suite", "contract-smoke")
    level = str(artifact.get("eval_level", ""))
    if level not in LEVELS:
        raise ValueError("schema v3 evaluation artifact requires a known eval_level")
    artifact["legacy_metrics"] = legacy
    return artifact


__all__ = ["LEVELS", "LevelDefinition", "classify_artifact"]
