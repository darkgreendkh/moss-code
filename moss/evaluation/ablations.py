"""L2 ablation contracts for context, cross-run memory, and crash recovery."""

import re
import unicodedata

from .stats import paired_bootstrap


CONTEXT_VARIANTS = frozenset(
    {"no_reduction", "truncate_only", "compaction", "compaction+offload"}
)
MEMORY_VARIANTS = frozenset(
    {"off", "episodic_only", "durable", "procedural", "irrelevant"}
)
MEMORY_DIMENSIONS = frozenset(
    {
        "information_extraction",
        "cross_session_reasoning",
        "temporal_update",
        "selective_forgetting",
        "abstention",
    }
)
RECOVERY_KILL_BOUNDARIES = (
    "before_intent",
    "after_intent_before_side_effect",
    "after_side_effect_before_receipt",
    "after_receipt_before_checkpoint",
)


def _require_real(row):
    if row.get("model_mode") != "real":
        raise ValueError("L2 ablation trials require model_mode=real")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _strings(nested)


def _tokens(value):
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.findall(r"[\w]+", normalized, flags=re.UNICODE)


def robust_fact_match(value, fact):
    """Match a fact despite case, punctuation, or harmless word-order changes."""
    fact_tokens = _tokens(fact)
    return bool(fact_tokens) and set(fact_tokens).issubset(set(_tokens(value)))


def assert_fact_absent(prompt_sections, critical_fact):
    """Reject self-proving memory trials before any provider call is made."""
    if any(robust_fact_match(section, critical_fact) for section in _strings(prompt_sections)):
        raise ValueError("self-proving memory trial: critical fact appears in prompt")
    return True


def _require_coverage(rows, field, expected):
    actual = {row.get(field) for row in rows}
    missing = set(expected) - actual
    unknown = actual - set(expected)
    if missing or unknown:
        raise ValueError(
            f"{field} coverage mismatch: missing={sorted(missing)} unknown={sorted(unknown, key=str)}"
        )


def _mean(rows, key):
    return sum(float(row[key]) for row in rows) / len(rows) if rows else 0.0


def summarize_context_trials(rows, *, baseline="no_reduction", iters=5000, seed=0):
    rows = [dict(row) for row in rows]
    if not rows:
        raise ValueError("context ablation requires trials")
    _require_coverage(rows, "variant", CONTEXT_VARIANTS)
    if baseline not in CONTEXT_VARIANTS:
        raise ValueError("unknown context baseline")
    for row in rows:
        _require_real(row)
        if row["variant"] in {"compaction", "compaction+offload"} and int(
            row.get("compactions", 0)
        ) < 2:
            raise ValueError("compaction variants must cross at least two compactions")
        if float(row.get("total_tokens", -1)) < 0 or float(row.get("wall_s", -1)) < 0:
            raise ValueError("token and latency metrics must be non-negative")
        row["passed"] = bool(row.get("passed"))
        row["information_retained"] = bool(row.get("information_retained"))
        row.setdefault("repeat", 0)

    by_variant = {variant: [row for row in rows if row["variant"] == variant] for variant in CONTEXT_VARIANTS}
    variants = {}
    for variant, variant_rows in by_variant.items():
        variants[variant] = {
            "n": len(variant_rows),
            "triplet": {
                "success_rate": _mean(variant_rows, "passed"),
                "avg_total_tokens": _mean(variant_rows, "total_tokens"),
                "avg_wall_s": _mean(variant_rows, "wall_s"),
            },
            "information_retention_rate": _mean(variant_rows, "information_retained"),
        }

    paired = {}
    baseline_rows = by_variant[baseline]
    for variant, variant_rows in by_variant.items():
        if variant == baseline:
            continue
        paired[variant] = {
            "success_rate": paired_bootstrap(
                baseline_rows,
                variant_rows,
                pair_key=("task_id", "repeat"),
                value_key="passed",
                iters=iters,
                seed=seed,
            ),
            "total_tokens": paired_bootstrap(
                baseline_rows,
                variant_rows,
                pair_key=("task_id", "repeat"),
                value_key="total_tokens",
                iters=iters,
                seed=seed,
            ),
            "wall_s": paired_bootstrap(
                baseline_rows,
                variant_rows,
                pair_key=("task_id", "repeat"),
                value_key="wall_s",
                iters=iters,
                seed=seed,
            ),
        }
    return {"eval_level": "L2", "baseline": baseline, "variants": variants, "paired_deltas": paired}


def summarize_memory_trials(rows):
    rows = [dict(row) for row in rows]
    if not rows:
        raise ValueError("memory ablation requires trials")
    _require_coverage(rows, "variant", MEMORY_VARIANTS)
    _require_coverage(rows, "dimension", MEMORY_DIMENSIONS)
    for row in rows:
        _require_real(row)
        if row.get("cross_run") is not True:
            raise ValueError("memory ablation trials must cross run boundaries")
        assert_fact_absent(row.get("prompt_sections", {}), row.get("critical_fact", ""))
        row["correct"] = bool(row.get("correct"))
        row["false_memory"] = bool(row.get("false_memory"))

    variants = {}
    for variant in MEMORY_VARIANTS:
        selected = [row for row in rows if row["variant"] == variant]
        variants[variant] = {
            "n": len(selected),
            "correct_rate": _mean(selected, "correct"),
            "false_memory_rate": _mean(selected, "false_memory"),
        }
    dimensions = {
        dimension: {
            "n": len(selected := [row for row in rows if row["dimension"] == dimension]),
            "correct_rate": _mean(selected, "correct"),
            "false_memory_rate": _mean(selected, "false_memory"),
        }
        for dimension in MEMORY_DIMENSIONS
    }
    return {"eval_level": "L2", "variants": variants, "dimensions": dimensions}


def summarize_recovery_trials(rows):
    rows = [dict(row) for row in rows]
    if not rows:
        raise ValueError("recovery ablation requires trials")
    _require_coverage(rows, "kill_boundary", RECOVERY_KILL_BOUNDARIES)
    for row in rows:
        _require_real(row)
        if min(
            int(row.get("duplicate_side_effects", -1)),
            int(row.get("extra_tokens", -1)),
            int(row.get("repeated_steps", -1)),
            int(row.get("baseline_steps", -1)),
        ) < 0:
            raise ValueError("recovery metrics must be non-negative")
    duplicate_side_effects = sum(int(row["duplicate_side_effects"]) for row in rows)
    baseline_steps = sum(int(row["baseline_steps"]) for row in rows)
    return {
        "eval_level": "L2",
        "n": len(rows),
        "completion_rate": _mean(rows, "completed"),
        "duplicate_side_effects": duplicate_side_effects,
        "safety_gate_passed": duplicate_side_effects == 0,
        "avg_extra_tokens": _mean(rows, "extra_tokens"),
        "repeated_work_rate": (
            sum(int(row["repeated_steps"]) for row in rows) / baseline_steps
            if baseline_steps
            else 0.0
        ),
    }
