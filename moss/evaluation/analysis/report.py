"""把各评测层分开渲染，禁止跨层混写结论。"""

import json

from ..levels import LEVELS, classify_artifact
from ..pricing import render_pareto


L2_COST_FIELDS = {
    "usd",
    "wall_s",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "model_turns",
    "tool_calls",
}


def _validate_claims(artifact):
    summary = dict(artifact.get("summary", {}) or {})
    for comparison in summary.get("comparisons", ()):
        has_interval = (
            isinstance(comparison.get("ci"), (list, tuple))
            and len(comparison["ci"]) == 2
        ) or {"ci_low", "ci_high"} <= set(comparison)
        if int(comparison.get("n", 0) or 0) <= 0 or not has_interval:
            raise ValueError("every comparison requires n and 95% CI")
    if artifact["eval_level"] == "L2":
        if not artifact.get("price_table_date"):
            raise ValueError("every L2 artifact requires price_table_date")
        for row in artifact.get("rows", ()):
            if not L2_COST_FIELDS <= set(row):
                raise ValueError("every L2 trial requires token, USD, latency, turn, and tool cost fields")
    if summary.get("incidents") == 0 and int(summary.get("n", 0) or 0) > 0:
        if "rule_of_three_upper" not in summary:
            raise ValueError("zero incidents require rule_of_three_upper")


def render_layered_report(artifacts):
    lines = ["# Moss Evaluation Report", ""]
    for raw_artifact in artifacts:
        artifact = classify_artifact(raw_artifact)
        _validate_claims(artifact)
        level = LEVELS[artifact["eval_level"]]
        if artifact["legacy_metrics"]:
            lines.extend(["> 旧版口径：按 L1 合同证据读取，不进入模型能力结论。", ""])
        summary = artifact.get("summary", {}) or {}
        cost_lines = []
        if artifact["eval_level"] == "L2":
            cost_lines = [
                f"Price table date: {artifact['price_table_date']}",
                (
                    f"Success / USD / wall_s: {float(summary.get('pass_rate', 0.0)):.2%} / "
                    f"${float(summary.get('avg_usd', 0.0)):.6f} / "
                    f"{float(summary.get('avg_wall_s', 0.0)):.3f}s"
                ),
                render_pareto(artifact.get("rows", ())),
                "",
            ]
        zero_event_line = []
        if summary.get("incidents") == 0 and int(summary.get("n", 0) or 0) > 0:
            zero_event_line = [
                f"0 incidents; 95% upper bound: {float(summary['rule_of_three_upper']):.2%}",
                "",
            ]
        lines.extend(
            [
                f"## {level.code} {level.name} — {artifact['suite']}",
                f"本层不能证明什么：{level.cannot_prove}",
                f"本层能证明什么：{level.can_prove}",
                "",
                *zero_event_line,
                *cost_lines,
                "```json",
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
