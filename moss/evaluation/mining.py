"""从本地 Git 历史挖掘可复现的 L2 编码任务。"""

import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

from ..clock import now
from .task_schema import validate_task


SOURCE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".kt"}


def _git(repo_root, *args, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        check=check,
        text=True,
    )
    return result.stdout.strip()


def _archive(repo_root, commit):
    result = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _extract_archive(payload, destination):
    destination = Path(destination).resolve()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"git archive member escapes destination: {member.name}")
        archive.extractall(destination)


def _show_file(repo_root, commit, relpath):
    result = subprocess.run(
        ["git", "show", f"{commit}:{relpath}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _is_test(path):
    path = str(path).replace("\\", "/")
    return path.startswith("tests/") or Path(path).name.startswith("test_")


def _is_source(path):
    return not _is_test(path) and Path(path).suffix.lower() in SOURCE_SUFFIXES


def _verify_workspace(workspace, test_paths, timeout_s=120):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *test_paths],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return result.returncode


def _slug(text):
    value = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return value[:40] or "task"


def _difficulty(source_paths):
    if len(source_paths) == 1:
        return "single_file"
    if len(source_paths) <= 4:
        return "multi_file"
    return "needs_iteration"


def _diagnose(diagnostics, commit, reason, details=""):
    if diagnostics is not None:
        diagnostics.append({"commit": commit, "reason": reason, "details": str(details)})


def mine_tasks(repo_root, *, since=None, limit=50, diagnostics=None, progress=None):
    repo_root = Path(repo_root).resolve()
    limit = max(1, int(limit))
    # limit 是“最终任务数”，不是“随便看前 N 个 commit”。有效任务通常只占
    # 源码+测试提交的一部分，因此扩大只读候选窗口，凑够目标后立即停止。
    candidate_limit = max(200, limit * 10)
    log_args = ["log", "--format=%H", f"--max-count={candidate_limit}"]
    if since:
        log_args.append(f"--since={since}")
    commits = _git(repo_root, *log_args).splitlines()
    tasks = []
    for index, commit in enumerate(commits, start=1):
        if len(tasks) >= limit:
            break
        if progress is not None:
            progress(index, len(commits), commit, len(tasks))
        parent_result = subprocess.run(
            ["git", "rev-parse", f"{commit}^"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if parent_result.returncode:
            _diagnose(diagnostics, commit, "no_parent")
            continue
        parent = parent_result.stdout.strip()
        changed = _git(repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines()
        tests = sorted(path for path in changed if _is_test(path))
        sources = sorted(path for path in changed if _is_source(path))
        if not tests or not sources:
            _diagnose(diagnostics, commit, "missing_source_or_test")
            continue
        try:
            parent_archive = _archive(repo_root, parent)
            commit_archive = _archive(repo_root, commit)
            with tempfile.TemporaryDirectory(prefix="moss-mine-") as temp_dir:
                root = Path(temp_dir)
                parent_workspace = root / "parent"
                commit_workspace = root / "commit"
                parent_workspace.mkdir()
                commit_workspace.mkdir()
                _extract_archive(parent_archive, parent_workspace)
                _extract_archive(commit_archive, commit_workspace)
                for test_path in tests:
                    destination = parent_workspace / test_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(_show_file(repo_root, commit, test_path))
                parent_codes = [_verify_workspace(parent_workspace, tests) for _ in range(3)]
                commit_codes = [_verify_workspace(commit_workspace, tests) for _ in range(3)]
        except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError) as exc:
            _diagnose(diagnostics, commit, "infra_failure", exc)
            continue
        if len(set(parent_codes)) > 1 or len(set(commit_codes)) > 1:
            _diagnose(diagnostics, commit, "flaky", {"parent": parent_codes, "commit": commit_codes})
            continue
        if parent_codes[0] == 0:
            _diagnose(diagnostics, commit, "parent_passed")
            continue
        if commit_codes[0] != 0:
            _diagnose(diagnostics, commit, "commit_failed", commit_codes[0])
            continue

        message = _git(repo_root, "show", "-s", "--format=%s", commit)
        task = {
            "schema_version": 2,
            "task_id": f"mined-{commit[:7]}-{_slug(message)}",
            "suite": "coding-mined",
            "eval_level": "L2",
            "raw_prompt": message,
            "prompt": message,
            "workspace": {
                "kind": "git_archive",
                "base_commit": parent,
                "overlay_paths": tests,
                "archive_sha256": "sha256:" + hashlib.sha256(parent_archive).hexdigest(),
            },
            "visible_tests": tests,
            "hidden_tests": [],
            "verifier": {
                "argv": ["python", "-m", "pytest", "-q", *tests],
                "cwd": ".",
                "clean_env": True,
                "timeout_s": 120,
                "network": "deny",
            },
            "allowed_tools": ["list_files", "read_file", "edit_file", "run_shell"],
            "budgets": {"step_budget": 12, "max_usd": 0.30, "max_seconds": 300},
            "difficulty": _difficulty(sources),
            "human_time_bucket": "minutes" if len(sources) <= 4 else "hours",
            "provenance": {
                "mined_from_commit": commit,
                "mined_at": now(),
                "min_model_cutoff": "2026-05",
                "contamination_status": "private",
            },
            "rubric": None,
            "status": "draft",
        }
        tasks.append(validate_task(task))
    return tasks
