import json
import subprocess
import sys

from moss.evaluation.audit import _apply_operator, audit_task, audit_task_bank
from moss.evaluation.mining import mine_tasks


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo_with_fix(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "audit@example.com")
    _git(repo, "config", "user.name", "Audit Test")
    (repo / "value.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_value.py").write_text(
        "from value import value\n\ndef test_value():\n    assert value() == 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial value")
    (repo / "value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_value.py").write_text(
        "from value import value\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix returned value")
    return repo


def _mined_task(repo):
    task = mine_tasks(repo, limit=1)[0]
    task["negative_patches"] = [
        {"name": "delete-key-assertion", "operator": "delete_assertion"},
        {"name": "surface-text-only", "operator": "surface_text"},
        {"name": "obvious-wrong", "operator": "obvious_wrong"},
    ]
    return task


def test_audit_rejects_all_three_negative_patches(tmp_path):
    repo = _repo_with_fix(tmp_path)

    result = audit_task(_mined_task(repo), repo)

    assert result.passed is True
    assert [item.name for item in result.negative_results] == [
        "delete-key-assertion",
        "surface-text-only",
        "obvious-wrong",
    ]
    assert all(item.rejected for item in result.negative_results)


def test_surviving_negative_patch_quarantines_task_and_invalidates_history(tmp_path):
    repo = _repo_with_fix(tmp_path)
    task = _mined_task(repo)
    task["verifier"]["argv"] = [sys.executable, "-c", "pass"]
    task_path = tmp_path / "tasks" / f"{task['task_id']}.json"
    task_path.parent.mkdir()
    task_path.write_text(json.dumps(task), encoding="utf-8")
    quarantine = tmp_path / "quarantine.jsonl"

    summary = audit_task_bank([task_path], repo_root=repo, quarantine_path=quarantine)

    assert summary["quarantined"] == 1
    assert summary["historical_conclusions_valid"] is False
    assert summary["invalidated_task_ids"] == [task["task_id"]]
    record = json.loads(quarantine.read_text(encoding="utf-8").splitlines()[0])
    assert record["task_id"] == task["task_id"]
    assert record["status"] == "quarantine"


def test_negative_patch_can_target_source_added_by_the_fix(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _apply_operator(workspace, "new_module.py", "obvious_wrong")

    assert (workspace / "new_module.py").read_text(encoding="utf-8") == (
        "raise RuntimeError('negative patch')\n"
    )
