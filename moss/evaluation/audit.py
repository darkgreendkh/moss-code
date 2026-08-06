"""对任务的负向补丁做 mutation 自检，并 append-only 记录 quarantine。"""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from ..atomic_io import append_line
from ..clock import now
from .mining import _archive, _extract_archive, _show_file
from .task_schema import validate_task
from .verifier import run_verification


@dataclass(frozen=True)
class NegativePatchResult:
    name: str
    operator: str
    rejected: bool
    verification: dict


@dataclass(frozen=True)
class TaskAudit:
    task_id: str
    passed: bool
    negative_results: tuple[NegativePatchResult, ...]

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "negative_results": [asdict(item) for item in self.negative_results],
        }


def _source_paths(repo_root, commit):
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return [
        path
        for path in result.stdout.splitlines()
        if path and not path.startswith("tests/") and Path(path).suffix in {".py", ".js", ".ts", ".go", ".rs", ".java", ".kt"}
    ]


def _apply_operator(workspace, source_path, operator):
    path = Path(workspace) / source_path
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    if operator == "delete_assertion":
        if not lines:
            path.write_text("# negative patch omitted implementation\n", encoding="utf-8")
            return
        index = next((i for i, line in enumerate(lines) if "assert" in line), None)
        if index is None:
            index = next((i for i, line in enumerate(lines) if "return" in line), 0)
        del lines[index]
        path.write_text("".join(lines), encoding="utf-8")
        return
    if operator == "surface_text":
        path.write_text(text + "\n# negative patch changed only surface text\n", encoding="utf-8")
        return
    if operator == "obvious_wrong":
        indentation = ""
        for line in lines:
            if line.lstrip().startswith(("return ", "return\n")):
                indentation = line[: len(line) - len(line.lstrip())]
                line_index = lines.index(line)
                lines[line_index] = f"{indentation}raise RuntimeError('negative patch')\n"
                break
        else:
            lines.insert(0, "raise RuntimeError('negative patch')\n")
        path.write_text("".join(lines), encoding="utf-8")
        return
    raise ValueError(f"unknown negative patch operator: {operator}")


def _fixture_workspace(task, repo_root, destination):
    _extract_archive(_archive(repo_root, task["workspace"]["base_commit"]), destination)
    commit = task["provenance"]["mined_from_commit"]
    for test_path in task["workspace"].get("overlay_paths", ()):
        target = Path(destination) / test_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_show_file(repo_root, commit, test_path))


def audit_task(task, repo_root):
    task = validate_task(task)
    repo_root = Path(repo_root).resolve()
    commit = task["provenance"]["mined_from_commit"]
    sources = _source_paths(repo_root, commit)
    if not sources:
        raise ValueError(f"task {task['task_id']} has no source path to mutate")
    patches = task.get("negative_patches", ())
    if len(patches) != 3:
        raise ValueError("coding task audit requires exactly three negative_patches")
    results = []
    with tempfile.TemporaryDirectory(prefix="moss-audit-") as temp_dir:
        fixture = Path(temp_dir) / "fixture"
        fixture.mkdir()
        _fixture_workspace(task, repo_root, fixture)
        for patch in patches:
            agent = Path(temp_dir) / f"agent-{patch['operator']}"
            shutil.copytree(fixture, agent)
            _apply_operator(agent, sources[0], patch["operator"])
            verification = run_verification(task, agent, fixture)
            results.append(
                NegativePatchResult(
                    name=str(patch["name"]),
                    operator=str(patch["operator"]),
                    rejected=not verification.passed,
                    verification=verification.to_dict(),
                )
            )
    return TaskAudit(
        task_id=task["task_id"],
        passed=all(item.rejected for item in results),
        negative_results=tuple(results),
    )


def _task_paths(paths):
    for value in paths:
        path = Path(value)
        if path.is_dir():
            yield from sorted(path.glob("mined-*.json"))
        else:
            yield path


def audit_task_bank(paths, *, repo_root, quarantine_path):
    audits = []
    invalidated = []
    for path in _task_paths(paths):
        task = json.loads(path.read_text(encoding="utf-8"))
        result = audit_task(task, repo_root)
        audits.append(result)
        if result.passed:
            continue
        invalidated.append(result.task_id)
        surviving = [item.name for item in result.negative_results if not item.rejected]
        append_line(
            quarantine_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "quarantine",
                    "task_id": result.task_id,
                    "recorded_at": now(),
                    "reason": "negative_patch_survived",
                    "surviving_patches": surviving,
                    "invalidate_historical_conclusions": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            force_fsync=True,
        )
    return {
        "audited": len(audits),
        "passed": sum(1 for item in audits if item.passed),
        "quarantined": len(invalidated),
        "invalidated_task_ids": invalidated,
        "historical_conclusions_valid": not invalidated,
    }
