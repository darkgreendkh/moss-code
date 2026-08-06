from pathlib import Path
import subprocess

from moss.evaluation import mining


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _build_fix_repo(tmp_path):
    repo = tmp_path / "history"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "eval@example.com")
    _git(repo, "config", "user.name", "Eval Test")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 1) == 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial broken calculator")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 1) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix add arithmetic")
    return repo


def test_mine_tasks_validates_parent_fail_commit_pass_and_preserves_caller(tmp_path, monkeypatch):
    repo = _build_fix_repo(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    marker = caller / "README.md"
    marker.write_text("do not touch\n", encoding="utf-8")
    monkeypatch.chdir(caller)

    diagnostics = []
    tasks = mining.mine_tasks(repo, limit=5, diagnostics=diagnostics)

    assert len(tasks) == 1
    task = tasks[0]
    assert task["prompt"] == "fix add arithmetic"
    assert task["workspace"]["overlay_paths"] == ["tests/test_calc.py"]
    assert task["workspace"]["archive_sha256"].startswith("sha256:")
    assert task["provenance"]["mined_from_commit"] == _git(repo, "rev-parse", "HEAD")
    assert Path.cwd() == caller
    assert marker.read_text(encoding="utf-8") == "do not touch\n"


def test_mine_tasks_excludes_flaky_validation_results(tmp_path, monkeypatch):
    repo = _build_fix_repo(tmp_path)
    calls = {"commit": 0}

    def flaky_runner(workspace, test_paths, timeout_s=120):
        del test_paths, timeout_s
        if Path(workspace).name == "parent":
            return 1
        calls["commit"] += 1
        return 1 if calls["commit"] == 2 else 0

    monkeypatch.setattr(mining, "_verify_workspace", flaky_runner)
    diagnostics = []

    assert mining.mine_tasks(repo, limit=5, diagnostics=diagnostics) == []
    assert any(item["reason"] == "flaky" for item in diagnostics)
