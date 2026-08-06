"""把各评测层分开渲染，禁止跨层混写结论。"""

import json

from ..levels import LEVELS, classify_artifact


def render_layered_report(artifacts):
    lines = ["# Moss Evaluation Report", ""]
    for raw_artifact in artifacts:
        artifact = classify_artifact(raw_artifact)
        level = LEVELS[artifact["eval_level"]]
        if artifact["legacy_metrics"]:
            lines.extend(["> 旧版口径：按 L1 合同证据读取，不进入模型能力结论。", ""])
        lines.extend(
            [
                f"## {level.code} {level.name} — {artifact['suite']}",
                f"本层不能证明什么：{level.cannot_prove}",
                f"本层能证明什么：{level.can_prove}",
                "",
                "```json",
                json.dumps(artifact.get("summary", {}), ensure_ascii=False, sort_keys=True),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
