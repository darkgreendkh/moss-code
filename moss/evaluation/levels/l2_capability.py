"""L2 真实模型能力工件的基础标记。"""

from ..pricing import PRICE_TABLE_DATE, TrialMetrics, estimate_cost


def capability_artifact(payload, suite="coding-mined"):
    artifact = dict(payload or {})
    artifact.update(
        {
            "schema_version": 3,
            "eval_level": "L2",
            "suite": str(suite),
            "price_table_date": PRICE_TABLE_DATE,
        }
    )
    return artifact


def trial_metrics(provider, model, values):
    values = dict(values or {})
    values["usd"] = estimate_cost(provider, model, values)
    return TrialMetrics.from_mapping(values)
