import pytest

from moss.evaluation.analysis.report import render_layered_report
from moss.evaluation.levels import LEVELS
from moss.evaluation.pricing import PRICE_TABLE_DATE


def test_all_evaluation_levels_define_proof_boundaries():
    assert set(LEVELS) == {"L0", "L1", "L2", "L3", "L4"}
    assert all(level.can_prove and level.cannot_prove for level in LEVELS.values())


def test_layered_report_prints_limitation_immediately_after_each_heading():
    report = render_layered_report(
        [
            {
                "schema_version": 3,
                "eval_level": "L1",
                "suite": "contract-smoke",
                "summary": {"pass_rate": 1.0},
            },
            {
                "schema_version": 3,
                    "eval_level": "L2",
                    "suite": "coding-mined",
                    "price_table_date": PRICE_TABLE_DATE,
                    "summary": {"pass_rate": 0.5},
            },
        ]
    )

    lines = report.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## L"):
            assert lines[index + 1].startswith("本层不能证明什么：")


def test_layered_report_marks_schema_v2_as_legacy_contract_evidence():
    report = render_layered_report(
        [{"schema_version": 2, "artifact_type": "harness-regression-v2", "summary": {}}]
    )

    assert "旧版口径" in report
    assert "## L1" in report
    assert "不能证明什么" in report


def test_layered_report_rejects_unclassified_schema_v3_artifact():
    with pytest.raises(ValueError, match="eval_level"):
        render_layered_report([{"schema_version": 3, "suite": "unknown", "summary": {}}])


def test_layered_report_rejects_comparison_without_n_and_interval():
    artifact = {
        "schema_version": 3,
        "eval_level": "L2",
        "suite": "coding-mined",
        "summary": {"comparisons": [{"label": "candidate-baseline", "delta": 0.1}]},
    }

    with pytest.raises(ValueError, match="n and 95% CI"):
        render_layered_report([artifact])


def test_layered_report_rejects_l2_trial_without_cost_fields():
    artifact = {
        "schema_version": 3,
        "eval_level": "L2",
        "suite": "coding-mined",
        "price_table_date": PRICE_TABLE_DATE,
        "summary": {},
        "rows": [{"task_id": "one", "passed": True}],
    }

    with pytest.raises(ValueError, match="cost fields"):
        render_layered_report([artifact])


def test_layered_report_requires_and_prints_rule_of_three_for_zero_incidents():
    invalid = {
        "schema_version": 3,
        "eval_level": "L3",
        "suite": "adversarial",
        "summary": {"incidents": 0, "n": 20},
    }
    with pytest.raises(ValueError, match="rule_of_three_upper"):
        render_layered_report([invalid])

    valid = {
        **invalid,
        "summary": {"incidents": 0, "n": 20, "rule_of_three_upper": 0.15},
    }
    report = render_layered_report([valid])
    assert "0 incidents; 95% upper bound: 15.00%" in report
