import pytest

from moss.evaluation.analysis.report import render_layered_report
from moss.evaluation.pricing import (
    PRICE_TABLE_DATE,
    TrialMetrics,
    equal_budget_summary,
    estimate_cost,
    pareto_frontier,
)


def test_unknown_model_cost_is_none_not_zero():
    assert estimate_cost("unknown", "mystery", {"input_tokens": 100, "output_tokens": 20}) is None


def test_known_prices_account_for_cache_and_output_tokens():
    openai = estimate_cost(
        "openai",
        "gpt-5-5",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 250_000,
            "cache_write_tokens": 0,
        },
    )
    anthropic = estimate_cost(
        "anthropic",
        "claude-opus-5",
        {
            "input_tokens": 750_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 250_000,
            "cache_write_tokens": 100_000,
        },
    )
    deepseek = estimate_cost(
        "deepseek",
        "deepseek-v4-pro",
        {
            "input_tokens": 750_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 250_000,
            "cache_write_tokens": 100_000,
        },
    )

    assert openai == pytest.approx(0.75 * 5 + 0.25 * 0.5 + 0.1 * 30)
    assert anthropic == pytest.approx(0.75 * 5 + 0.25 * 0.5 + 0.1 * 10 + 0.1 * 25)
    assert deepseek == pytest.approx(0.75 * 0.435 + 0.25 * 0.003625 + 0.1 * 0.435 + 0.1 * 0.87)


def test_trial_metrics_exposes_every_cost_dimension():
    trial = TrialMetrics.from_mapping(
        {
            "passed": True,
            "usd": 0.2,
            "wall_s": 4.5,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 30,
            "cache_write_tokens": 5,
            "model_turns": 2,
            "tool_calls": 3,
        }
    )

    assert trial.to_dict()["usd"] == 0.2
    assert trial.total_tokens == 155


def test_equal_budget_summary_counts_only_completed_trials_inside_budget():
    rows = [
        {"passed": True, "usd": 0.1, "input_tokens": 100, "output_tokens": 20},
        {"passed": False, "usd": 0.2, "input_tokens": 80, "output_tokens": 10},
        {"passed": True, "usd": 0.8, "input_tokens": 50, "output_tokens": 10},
    ]

    summary = equal_budget_summary(rows, budget_usd=0.5, budget_tokens=150)

    assert summary == {"n": 3, "within_budget": 2, "completed": 1, "completion_rate": 0.5}


def test_pareto_frontier_removes_trials_dominated_on_success_cost_and_latency():
    rows = [
        {"task_id": "best", "passed": True, "usd": 0.1, "wall_s": 2.0},
        {"task_id": "slow", "passed": True, "usd": 0.1, "wall_s": 3.0},
        {"task_id": "cheap-fail", "passed": False, "usd": 0.05, "wall_s": 1.0},
    ]

    assert [row["task_id"] for row in pareto_frontier(rows)] == ["best", "cheap-fail"]


def test_l2_report_includes_price_date_triplet_and_text_pareto():
    row = {
        "task_id": "one",
        "passed": True,
        "usd": 0.1,
        "wall_s": 2.0,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 10,
        "cache_write_tokens": 0,
        "model_turns": 2,
        "tool_calls": 3,
    }
    report = render_layered_report(
        [
            {
                "schema_version": 3,
                "eval_level": "L2",
                "suite": "coding-mined",
                "price_table_date": PRICE_TABLE_DATE,
                "summary": {"pass_rate": 1.0, "avg_usd": 0.1, "avg_wall_s": 2.0},
                "rows": [row],
            }
        ]
    )

    assert f"Price table date: {PRICE_TABLE_DATE}" in report
    assert "Success / USD / wall_s: 100.00% / $0.100000 / 2.000s" in report
    assert "Pareto:" in report
