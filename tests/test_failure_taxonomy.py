import pytest

from moss.evaluation.failure_taxonomy import LABELS, label_trial, summarize_failure_labels
from moss.evaluation.analysis.trajectory import analyze_trajectories


CASES = {
    "wrong_file_targeted": (
        [],
        {"expected_paths": ["src/right.py"]},
        ["src/wrong.py"],
    ),
    "never_read_before_edit": (
        [{"event": "tool_call", "tool": "write_file", "args": {"path": "src/a.py"}}],
        {},
        [],
    ),
    "no_plan": (
        [{"event": "tool_call", "tool": "edit_file", "args": {"path": "src/a.py"}}],
        {},
        [],
    ),
    "plan_drift": ([{"event": "plan_step", "status": "drift"}], {}, []),
    "premature_final": ([{"event": "final", "verified": False}], {}, []),
    "invalid_args_repeat": (
        [
            {"event": "tool_error", "tool": "read_file", "error_code": "invalid_args"},
            {"event": "tool_error", "tool": "read_file", "error_code": "invalid_args"},
        ],
        {},
        [],
    ),
    "unknown_tool": ([{"event": "tool_error", "error_code": "unknown_tool"}], {}, []),
    "tool_arg_hallucination": (
        [{"event": "tool_error", "error_code": "invalid_args", "unknown_arguments": ["magical"]}],
        {},
        [],
    ),
    "no_progress_loop": (
        [{"event": "tool_call", "tool": "read_file", "args": {"path": "a"}}] * 3,
        {},
        [],
    ),
    "ab_loop": (
        [{"event": "model_turn", "fingerprint": value} for value in ("A", "B", "A", "B")],
        {},
        [],
    ),
    "retry_storm": (
        [{"event": "tool_error", "tool": "run_shell", "error_code": "failed"}] * 3,
        {},
        [],
    ),
    "context_overflow": ([{"event": "model_error", "error_code": "context_overflow"}], {}, []),
    "error_signal_lost": (
        [
            {"event": "tool_error", "error_code": "failed"},
            {"event": "final", "verified": True, "acknowledged_error": False},
        ],
        {},
        [],
    ),
    "forgot_constraint": ([{"event": "constraint_violation"}], {}, []),
    "path_escape_attempt": ([{"event": "tool_error", "error_code": "path_escape"}], {}, []),
    "approval_denied_then_gave_up": (
        [
            {"event": "capability_denied"},
            {"event": "final", "verified": False},
        ],
        {},
        [],
    ),
    "prompt_injection_followed": ([{"event": "prompt_injection", "followed": True}], {}, []),
    "reward_hack": ([{"event": "verification", "labels": ["reward_hack"]}], {}, []),
    "corrupt_success": ([{"event": "verification", "labels": ["corrupt_success"]}], {}, []),
    "env_missing_dep": ([{"event": "tool_error", "error_code": "missing_dependency"}], {}, []),
    "timeout": ([{"event": "tool_error", "error_code": "timeout"}], {}, []),
    "infra_failure": ([{"event": "runner_error", "infra_failure": True}], {}, []),
}


@pytest.mark.parametrize("label", sorted(CASES))
def test_each_failure_label_has_a_positive_rule(label):
    events, task, diff = CASES[label]

    assert label in label_trial(events, task, diff)


@pytest.mark.parametrize("label", sorted(CASES))
def test_each_failure_label_has_a_negative_case(label):
    assert label not in label_trial([{"event": "verification", "status": "pass"}], {}, [])


def test_taxonomy_matches_the_spec_and_output_order_is_stable():
    assert set(CASES) == set(LABELS)

    labels = label_trial(
        [
            {"event": "tool_error", "error_code": "timeout"},
            {"event": "runner_error", "infra_failure": True},
        ],
        {},
        [],
    )

    assert labels == ["timeout", "infra_failure"]


def test_failure_summary_is_a_histogram_with_run_id_drilldown():
    summary = summarize_failure_labels(
        [
            {"run_id": "run-1", "labels": ["timeout", "infra_failure"]},
            {"run_id": "run-2", "labels": ["timeout"]},
            {"run_id": "run-3", "labels": []},
        ]
    )

    assert summary["failed_runs"] == 2
    assert summary["histogram"]["timeout"] == 2
    assert summary["run_ids_by_label"]["timeout"] == ["run-1", "run-2"]


def test_trajectory_analysis_reports_automatic_label_coverage():
    summary = analyze_trajectories(
        [
            {
                "run_id": "run-1",
                "status": "fail",
                "trace_events": [{"event": "tool_error", "error_code": "timeout"}],
            },
            {"run_id": "run-2", "status": "fail", "trace_events": []},
        ]
    )

    assert summary["automatic_label_coverage"] == 0.5
