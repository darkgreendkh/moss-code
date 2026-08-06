import pytest

from moss.evaluation.judge import (
    JudgeVerdict,
    build_blinded_judge_prompt,
    calibrate_judge,
    cohen_kappa,
    combine_verifier_and_judge,
    judge_budget_guard,
    load_gold_slots,
)


def test_judge_verdict_is_structured_and_cannot_override_binary_verifier():
    verdict = JudgeVerdict.from_mapping(
        {
            "score": 0.9,
            "rubric_hits": ["clear"],
            "rationale": "Strong explanation",
            "judge_model": "judge-v1",
            "judge_prompt_sha": "sha256:abc",
        }
    )

    combined = combine_verifier_and_judge(False, verdict, calibrated=True)

    assert combined["binary_pass"] is False
    assert combined["partial_score"] == 0.9
    assert combined["judge_calibrated"] is True


def test_judge_prompt_is_blinded_and_uses_task_specific_rubric():
    prompt = build_blinded_judge_prompt(
        task={"task_id": "t1", "variant": "new", "rubric": ["mentions rollback"]},
        trace_summary={"provider": "secret-provider", "steps": 3},
        final_answer="Use rollback.",
        reference_answer="Rollback is required.",
    )

    assert "mentions rollback" in prompt
    assert "secret-provider" not in prompt
    assert '"variant"' not in prompt
    assert '"provider"' not in prompt


def test_cohen_kappa_and_calibration_threshold():
    assert cohen_kappa(["pass"] * 50, ["pass"] * 50) == 1.0
    rows = [
        {"slot_id": f"g-{index}", "human_label": index % 2, "judge_label": index % 2}
        for index in range(50)
    ]

    calibration = calibrate_judge(rows, judge_model="judge-v1", judge_prompt_sha="sha256:p1")

    assert calibration["status"] == "calibrated"
    assert calibration["kappa"] == 1.0
    assert calibration["correlation"] == 1.0
    assert calibration["calibration_sha"].startswith("sha256:")


def test_calibration_below_threshold_is_explicitly_uncalibrated():
    rows = [
        {"slot_id": f"g-{index}", "human_label": index % 2, "judge_label": (index + 1) % 2}
        for index in range(50)
    ]

    assert calibrate_judge(rows, judge_model="judge-v1", judge_prompt_sha="sha256:p1")[
        "status"
    ] == "uncalibrated"


def test_judge_cost_guard_requires_downsampling_above_fifteen_percent():
    assert judge_budget_guard(capability_usd=85, judge_usd=15)["within_guardrail"] is True
    assert judge_budget_guard(capability_usd=80, judge_usd=20)["within_guardrail"] is False


def test_gold_scaffold_has_fifty_pending_blind_slots_not_fake_labels():
    slots = load_gold_slots("benchmarks/gold/judge-calibration-v1.json")

    assert len(slots) == 50
    assert all(slot["status"] == "pending_blind_annotation" for slot in slots)
    assert all("human_label" not in slot and "judge_label" not in slot for slot in slots)


def test_judge_rejects_out_of_range_scores():
    with pytest.raises(ValueError, match="between zero and one"):
        JudgeVerdict.from_mapping(
            {
                "score": 1.1,
                "rubric_hits": [],
                "rationale": "bad",
                "judge_model": "judge-v1",
                "judge_prompt_sha": "sha256:p1",
            }
        )
