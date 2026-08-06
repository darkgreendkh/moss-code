import pytest

from moss.evaluation.ablations import (
    CONTEXT_VARIANTS,
    MEMORY_DIMENSIONS,
    MEMORY_VARIANTS,
    RECOVERY_KILL_BOUNDARIES,
    summarize_context_trials,
    summarize_memory_trials,
    summarize_recovery_trials,
)


def _context(task_id, variant, passed, tokens, wall_s, compactions=0):
    return {
        "task_id": task_id,
        "repeat": 0,
        "variant": variant,
        "model_mode": "real",
        "passed": passed,
        "total_tokens": tokens,
        "wall_s": wall_s,
        "compactions": compactions,
        "information_retained": passed,
    }


def test_context_ablation_requires_all_variants_and_reports_paired_triplets():
    rows = []
    for task_id, base in (("a", 100), ("b", 200)):
        rows.extend(
            [
                _context(task_id, "no_reduction", True, base + 50, 4.0),
                _context(task_id, "truncate_only", False, base, 3.0),
                _context(task_id, "compaction", True, base - 20, 2.0, 2),
                _context(task_id, "compaction+offload", True, base - 30, 1.5, 3),
            ]
        )

    summary = summarize_context_trials(rows, baseline="no_reduction", iters=100, seed=7)

    assert set(summary["variants"]) == CONTEXT_VARIANTS
    assert summary["variants"]["compaction"]["triplet"] == {
        "success_rate": 1.0,
        "avg_total_tokens": 130.0,
        "avg_wall_s": 2.0,
    }
    assert summary["variants"]["compaction"]["information_retention_rate"] == 1.0
    assert summary["paired_deltas"]["compaction"]["success_rate"]["n"] == 2


def test_context_compaction_variant_requires_two_actual_compactions():
    rows = [_context("a", variant, True, 10, 1, 1) for variant in CONTEXT_VARIANTS]

    with pytest.raises(ValueError, match="at least two compactions"):
        summarize_context_trials(rows)


def test_memory_ablation_covers_variants_dimensions_and_false_memory():
    rows = []
    for variant in MEMORY_VARIANTS:
        for dimension in MEMORY_DIMENSIONS:
            rows.append(
                {
                    "task_id": f"{variant}-{dimension}",
                    "variant": variant,
                    "dimension": dimension,
                    "model_mode": "real",
                    "cross_run": True,
                    "prompt_sections": {"task": "Recall the stored preference.", "history": []},
                    "critical_fact": "database is sqlite",
                    "correct": variant != "off",
                    "false_memory": variant == "irrelevant",
                }
            )

    summary = summarize_memory_trials(rows)

    assert set(summary["variants"]) == MEMORY_VARIANTS
    assert set(summary["dimensions"]) == MEMORY_DIMENSIONS
    assert summary["variants"]["irrelevant"]["false_memory_rate"] == 1.0


def test_recovery_ablation_requires_four_kill_boundaries_and_zero_duplicate_side_effects():
    rows = [
        {
            "task_id": boundary,
            "kill_boundary": boundary,
            "model_mode": "real",
            "completed": True,
            "duplicate_side_effects": 0,
            "extra_tokens": 10,
            "repeated_steps": 1,
            "baseline_steps": 10,
        }
        for boundary in RECOVERY_KILL_BOUNDARIES
    ]

    summary = summarize_recovery_trials(rows)

    assert summary["completion_rate"] == 1.0
    assert summary["duplicate_side_effects"] == 0
    assert summary["avg_extra_tokens"] == 10.0
    assert summary["repeated_work_rate"] == 0.1


def test_recovery_duplicate_side_effect_is_a_failed_safety_gate():
    rows = [
        {
            "task_id": boundary,
            "kill_boundary": boundary,
            "model_mode": "real",
            "completed": True,
            "duplicate_side_effects": int(index == 0),
            "extra_tokens": 0,
            "repeated_steps": 0,
            "baseline_steps": 1,
        }
        for index, boundary in enumerate(RECOVERY_KILL_BOUNDARIES)
    ]

    summary = summarize_recovery_trials(rows)

    assert summary["safety_gate_passed"] is False

