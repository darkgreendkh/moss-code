from pathlib import Path

import pytest

from moss.evaluation.adversarial import (
    ADVERSARIAL_VARIANTS,
    DEFAULT_SCENARIO_MATRIX,
    adversarial_summary,
    load_scenarios,
    materialize_scenario,
)


def test_default_matrix_expands_to_thirty_unique_scenarios():
    scenarios = load_scenarios(DEFAULT_SCENARIO_MATRIX)

    assert len(scenarios) == 30
    assert len({scenario["scenario_id"] for scenario in scenarios}) == 30
    assert {scenario["surface"] for scenario in scenarios} == {
        "readme",
        "code_comment",
        "test_output",
        "package_description",
        "agents_md",
    }
    assert {scenario["target"] for scenario in scenarios} == {
        "read_env",
        "exfiltrate",
        "modify_ci",
        "memory_backdoor",
        "bypass_approval",
        "path_escape",
    }


@pytest.mark.parametrize(
    ("surface", "expected_path"),
    [
        ("readme", "README.md"),
        ("code_comment", "src/injection_fixture.py"),
        ("test_output", "tests/fixtures/injected-output.txt"),
        ("package_description", "package.json"),
        ("agents_md", "AGENTS.md"),
    ],
)
def test_each_injection_surface_materializes_only_in_scratch_workspace(tmp_path, surface, expected_path):
    scenario = next(
        item for item in load_scenarios(DEFAULT_SCENARIO_MATRIX) if item["surface"] == surface
    )

    path = materialize_scenario(scenario, tmp_path)

    assert path.relative_to(tmp_path) == Path(expected_path)
    assert scenario["payload"] in path.read_text(encoding="utf-8")


def test_adversarial_materialization_rejects_real_checkout():
    scenario = load_scenarios(DEFAULT_SCENARIO_MATRIX)[0]

    with pytest.raises(ValueError, match="scratch workspace"):
        materialize_scenario(scenario, Path.cwd())


def test_adversarial_metrics_keep_safety_and_utility_visible_together():
    rows = []
    for variant in ADVERSARIAL_VARIANTS:
        rows.append(
            {
                "scenario_id": f"scenario-{variant}",
                "variant": variant,
                "attack_succeeded": variant == "off",
                "refused_attack": variant != "off",
                "utility_completed": variant != "annotation+detection",
                "false_positive": variant == "annotation_only",
                "approval_count": 2 if variant == "full" else 0,
            }
        )

    summary = adversarial_summary(rows)

    assert set(summary["variants"]) == ADVERSARIAL_VARIANTS
    assert summary["variants"]["off"]["attack_success_rate"] == 1.0
    assert summary["variants"]["full"]["utility_retention"] == 1.0
    assert summary["variants"]["full"]["approval_burden"] == 2.0
    assert summary["safety_utility_curve"][0].keys() >= {
        "variant",
        "attack_success_rate",
        "utility_retention",
    }


def test_zero_attack_success_reports_rule_of_three_upper_bound():
    rows = [
        {
            "scenario_id": str(index),
            "variant": variant,
            "attack_succeeded": False,
            "refused_attack": True,
            "utility_completed": True,
            "false_positive": False,
            "approval_count": 0,
        }
        for variant in ADVERSARIAL_VARIANTS
        for index in range(30)
    ]

    summary = adversarial_summary(rows)

    assert summary["variants"]["full"]["attack_success_upper_95"] == 0.1

