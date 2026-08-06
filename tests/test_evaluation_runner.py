import json
from pathlib import Path

from moss.evaluation.manifest import RunManifest
from moss.evaluation.runner import regression_decision, run_parallel_trials


def _worker(task, workspace):
    workspace = Path(workspace)
    assert (workspace / ".moss").is_dir()
    if task["outcome"] == "infra":
        raise RuntimeError("provider disconnected")
    return {
        "task_id": task["task_id"],
        "status": task["outcome"],
        "workspace": str(workspace),
    }


def _inspect_worker(task, workspace):
    del workspace
    payload = json.loads(Path(task["artifact_path"]).read_text(encoding="utf-8"))
    return {"task_id": "probe", "status": "pass", "saw_status": payload["status"]}


def _manifest(workers=2):
    return RunManifest.capture(
        repo_root=".",
        prompt_version="p3",
        tool_schema={},
        policy_version="policy-v2",
        provider="fake",
        model="scripted",
        decoding={},
        taskset=b"tasks",
        fixture=b"fixture",
        split="test",
        max_steps=2,
        budgets={},
        workers=workers,
        sandbox="process",
    )


def test_parallel_runner_isolates_workspaces_and_separates_infra_failures(tmp_path):
    artifact_path = tmp_path / "artifact.json"
    tasks = [
        {"task_id": "pass", "outcome": "pass"},
        {"task_id": "fail", "outcome": "fail"},
        {"task_id": "infra", "outcome": "infra"},
    ]

    artifact = run_parallel_trials(
        tasks,
        _worker,
        manifest=_manifest(),
        artifact_path=artifact_path,
        rerun_policy={"max_infra_retries": 1, "backoff_s": 0},
    )

    assert artifact["status"] == "complete"
    assert artifact["summary"] == {
        "started_trials": 3,
        "valid_trials": 2,
        "capability_passes": 1,
        "infra_failures": 1,
        "capability_rate_valid_environment": 0.5,
        "end_to_end_reliability": 1 / 3,
    }
    assert len({row["workspace"] for row in artifact["rows"] if "workspace" in row}) == 2
    assert artifact["manifest"]["workers"] == 2
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == artifact
    assert artifact["attempts"]["infra"][0]["status"] == "infra_failure"
    assert artifact["attempts"]["infra"][1]["status"] == "infra_failure"


def test_artifact_is_marked_incomplete_before_workers_start(tmp_path):
    artifact_path = tmp_path / "artifact.json"

    artifact = run_parallel_trials(
        [{"task_id": "probe", "artifact_path": str(artifact_path)}],
        _inspect_worker,
        manifest=_manifest(workers=1),
        artifact_path=artifact_path,
    )

    assert artifact["rows"][0]["saw_status"] == "incomplete"


def test_regression_blocks_only_when_sample_and_interval_support_it():
    assert regression_decision({"n": 5, "estimate": -0.5, "ci_low": -0.8, "ci_high": -0.2})[
        "status"
    ] == "warning"
    assert regression_decision(
        {"n": 30, "estimate": -0.2, "ci_low": -0.3, "ci_high": -0.1}
    )["status"] == "fail"
    assert regression_decision(
        {"n": 30, "estimate": -0.1, "ci_low": -0.3, "ci_high": 0.1}
    )["status"] == "pass"
