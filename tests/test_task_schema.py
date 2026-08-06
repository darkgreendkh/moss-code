import json

import pytest

from moss.evaluation.task_schema import lint_task_paths, validate_task


def _valid_task():
    return {
        "schema_version": 2,
        "task_id": "mined-abc1234-fix-add",
        "suite": "coding-mined",
        "eval_level": "L2",
        "prompt": "Fix add for negative values",
        "workspace": {
            "kind": "git_archive",
            "base_commit": "abc1234^",
            "overlay_paths": ["tests/test_math.py"],
            "archive_sha256": "sha256:" + "a" * 64,
        },
        "visible_tests": ["tests/test_math.py"],
        "hidden_tests": [],
        "verifier": {
            "argv": ["python", "-m", "pytest", "-q", "tests/test_math.py"],
            "cwd": ".",
            "clean_env": True,
            "timeout_s": 120,
            "network": "deny",
        },
        "allowed_tools": ["list_files", "read_file", "edit_file", "run_shell"],
        "budgets": {"step_budget": 12, "max_usd": 0.3, "max_seconds": 300},
        "difficulty": "single_file",
        "human_time_bucket": "minutes",
        "provenance": {
            "mined_from_commit": "abc1234",
            "mined_at": "2026-08-06T00:00:00+00:00",
            "min_model_cutoff": "2026-05",
            "contamination_status": "private",
        },
        "rubric": None,
        "status": "draft",
    }


def test_task_schema_accepts_complete_v2_task():
    normalized = validate_task(_valid_task())

    assert normalized["schema_version"] == 2
    assert normalized["verifier"]["argv"][0] == "python"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda task: task.pop("prompt"), "prompt"),
        (lambda task: task.update(eval_level="L1"), "eval_level"),
        (lambda task: task["workspace"].update(kind="checkout"), "workspace.kind"),
        (lambda task: task["verifier"].update(argv="python -m pytest"), "verifier.argv"),
        (lambda task: task["verifier"].update(cwd="../outside"), "verifier.cwd"),
        (lambda task: task.update(visible_tests=["../secret.py"]), "visible_tests"),
    ],
)
def test_task_schema_rejects_bad_tasks(mutate, message):
    task = _valid_task()
    mutate(task)

    with pytest.raises(ValueError, match=message):
        validate_task(task)


def test_lint_task_paths_reports_each_bad_file(tmp_path):
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    good.write_text(json.dumps(_valid_task()), encoding="utf-8")
    bad.write_text("{}", encoding="utf-8")

    errors = lint_task_paths([tmp_path])

    assert len(errors) == 1
    assert str(bad) in errors[0]
