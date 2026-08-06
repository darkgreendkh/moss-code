"""L2 真实模型能力工件的基础标记。"""


def capability_artifact(payload, suite="coding-mined"):
    artifact = dict(payload or {})
    artifact.update({"schema_version": 3, "eval_level": "L2", "suite": str(suite)})
    return artifact
