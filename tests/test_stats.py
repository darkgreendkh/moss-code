import math
import random

import pytest

from moss.evaluation.stats import (
    cluster_bootstrap,
    paired_bootstrap,
    pass_hat_k,
    rule_of_three,
    success_at_k,
    wilson_interval,
)


def _mean_passed(rows):
    return sum(float(row["passed"]) for row in rows) / len(rows)


def test_wilson_interval_matches_hand_checked_half_success_case():
    low, high = wilson_interval(5, 10)

    assert low == pytest.approx(0.2366, abs=0.0001)
    assert high == pytest.approx(0.7634, abs=0.0001)


def test_wilson_interval_has_expected_simulated_coverage():
    rng = random.Random(7)
    covered = 0
    trials = 1000
    for _ in range(trials):
        successes = sum(rng.random() < 0.35 for _ in range(40))
        low, high = wilson_interval(successes, 40)
        covered += low <= 0.35 <= high

    assert covered / trials == pytest.approx(0.95, abs=0.03)


def test_pass_k_estimators_match_combinatorial_definitions():
    assert pass_hat_k(5, 3, 2) == pytest.approx(math.comb(3, 2) / math.comb(5, 2))
    assert success_at_k(5, 3, 2) == pytest.approx(1 - math.comb(2, 2) / math.comb(5, 2))
    assert pass_hat_k(5, 1, 2) == 0.0
    assert success_at_k(5, 5, 2) == 1.0


def test_cluster_bootstrap_resamples_clusters_and_returns_a_bounded_interval():
    rows = [
        {"repo": "a", "task": "1", "passed": 1},
        {"repo": "a", "task": "2", "passed": 1},
        {"repo": "b", "task": "3", "passed": 0},
        {"repo": "b", "task": "4", "passed": 1},
    ]

    result = cluster_bootstrap(
        rows,
        _mean_passed,
        cluster_key=("repo", "task"),
        iters=800,
        seed=11,
    )

    assert result["estimate"] == 0.75
    assert 0.0 <= result["ci_low"] <= result["estimate"] <= result["ci_high"] <= 1.0
    assert result["n"] == 4


def test_paired_bootstrap_preserves_pairs_and_finds_known_positive_delta():
    baseline = [{"pair": str(index), "passed": index % 2} for index in range(20)]
    candidate = [{"pair": str(index), "passed": 1} for index in range(20)]

    result = paired_bootstrap(baseline, candidate, pair_key="pair", iters=1000, seed=3)

    assert result["estimate"] == 0.5
    assert result["ci_low"] > 0
    assert result["n"] == 20


def test_rule_of_three_reports_zero_event_upper_bound():
    assert rule_of_three(20) == 0.15
    with pytest.raises(ValueError, match="positive"):
        rule_of_three(0)
