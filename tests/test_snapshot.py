"""增量快照（spec-01 §4.3）的行为测试。"""

import shutil
import subprocess

import pytest

from moss.ignore import IgnoreRules
from moss.workspace import capture_snapshot, diff_snapshots

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def _capture(tmp_path, previous=None, strategy="git"):
    return capture_snapshot(
        tmp_path,
        strategy=strategy,
        git_changed=tuple(previous or ()),
        detailed=True,
    )


def test_git_strategy_detects_create_modify_delete(tmp_path):
    _init_repo(tmp_path)
    before = _capture(tmp_path)

    (tmp_path / "new.txt").write_text("fresh\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").unlink()
    after = _capture(tmp_path, previous=before.entries)

    changed, summaries = diff_snapshots(before.entries, after.entries, untracked=after.untracked)

    assert after.strategy == "git"
    assert set(changed) == {"new.txt", "tracked.txt", "pkg/mod.py"}
    assert "created:new.txt" in summaries
    assert "modified:tracked.txt" in summaries
    assert "deleted:pkg/mod.py" in summaries


def test_git_strategy_only_stats_the_changed_set(tmp_path):
    """增量快照的 key 集合必须是 O(变更集)，不是 O(全仓)。"""
    _init_repo(tmp_path)
    for index in range(50):
        (tmp_path / f"bulk{index}.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "bulk"], cwd=tmp_path, check=True)

    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    snapshot = _capture(tmp_path)

    assert set(snapshot.entries) == {"tracked.txt"}


def test_git_and_walk_strategies_agree_on_shared_paths(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("new\n", encoding="utf-8")

    git_snapshot = capture_snapshot(tmp_path, strategy="git")
    walk_snapshot = capture_snapshot(tmp_path, strategy="walk", ignore=IgnoreRules.load(tmp_path))

    shared = set(git_snapshot) & set(walk_snapshot)
    assert shared == set(git_snapshot)
    for path in shared:
        assert git_snapshot[path] == walk_snapshot[path]


def test_symlink_is_recorded_instead_of_skipped(tmp_path):
    """软链换目标是真实的改动，原来的实现直接跳过 symlink，等于隐身。"""
    _init_repo(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(tmp_path / "tracked.txt")

    before = capture_snapshot(tmp_path, strategy="walk")
    link.unlink()
    link.symlink_to(outside)
    after = capture_snapshot(tmp_path, strategy="walk")

    assert before["link.txt"][0] == "symlink"
    changed, _ = diff_snapshots(before, after)
    assert "link.txt" in changed


def test_walk_strategy_prunes_ignored_directories(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("x", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")

    snapshot = capture_snapshot(tmp_path, strategy="walk", ignore=IgnoreRules.load(tmp_path))

    assert "keep.txt" in snapshot
    assert not any(path.startswith("build/") for path in snapshot)


def test_git_strategy_falls_back_to_walk_and_says_so(tmp_path):
    """没有 .git 时显式要 git 策略，要如实标注降级，评测口径才看得见。"""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    result = capture_snapshot(tmp_path, strategy="git", detailed=True)

    assert result.strategy == "walk_only"
    assert set(result.entries) == {"a.txt"}
