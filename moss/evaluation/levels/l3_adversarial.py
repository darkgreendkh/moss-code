"""L3 对抗评测工件的基础标记。"""


def adversarial_artifact(payload, suite="adversarial"):
    artifact = dict(payload or {})
    artifact.update({"schema_version": 3, "eval_level": "L3", "suite": str(suite)})
    return artifact
