"""Task schema v2 的 stdlib 校验器；JSON Schema 文件只作跨工具说明。"""

import json
from pathlib import Path, PurePosixPath

from ..tools import legal_tool_names
from .verifier import ExecutableSpec


REQUIRED_KEYS = {
    "schema_version",
    "task_id",
    "suite",
    "eval_level",
    "prompt",
    "workspace",
    "visible_tests",
    "hidden_tests",
    "verifier",
    "allowed_tools",
    "budgets",
    "difficulty",
    "human_time_bucket",
    "provenance",
    "rubric",
}
SUITES = {"contract-smoke", "coding-mined", "memory", "context", "recovery", "adversarial"}
DIFFICULTIES = {"single_file", "multi_file", "needs_iteration"}
HUMAN_TIME_BUCKETS = {"minutes", "hours"}


def _safe_path(value, field):
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} contains an unsafe path: {value!r}")
    return text


def _path_list(value, field, *, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{field} must be a {'possibly empty ' if allow_empty else 'non-empty '}list")
    return [_safe_path(item, field) for item in value]


def validate_task(value):
    if not isinstance(value, dict):
        raise ValueError("task must be a mapping")
    missing = sorted(REQUIRED_KEYS - set(value))
    if missing:
        raise ValueError(f"task is missing required fields: {', '.join(missing)}")
    task = json.loads(json.dumps(value))
    if int(task["schema_version"]) != 2:
        raise ValueError("schema_version must be 2")
    task_id = str(task["task_id"]).strip()
    if not task_id:
        raise ValueError("task_id must not be empty")
    task["task_id"] = task_id
    if task["suite"] not in SUITES:
        raise ValueError("suite is not recognized")
    if task["eval_level"] not in {"L1", "L2", "L3"}:
        raise ValueError("eval_level must be L1, L2, or L3")
    if task["suite"] == "coding-mined" and task["eval_level"] != "L2":
        raise ValueError("coding-mined eval_level must be L2")
    task["prompt"] = str(task["prompt"]).strip()
    if not task["prompt"]:
        raise ValueError("prompt must not be empty")

    workspace = task["workspace"]
    if not isinstance(workspace, dict) or workspace.get("kind") not in {"fixture", "git_archive"}:
        raise ValueError("workspace.kind must be fixture or git_archive")
    if workspace["kind"] == "git_archive":
        for key in ("base_commit", "overlay_paths", "archive_sha256"):
            if key not in workspace:
                raise ValueError(f"workspace.{key} is required")
        workspace["base_commit"] = str(workspace["base_commit"]).strip()
        workspace["overlay_paths"] = _path_list(
            workspace["overlay_paths"], "workspace.overlay_paths", allow_empty=True
        )
        digest = str(workspace["archive_sha256"])
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError("workspace.archive_sha256 must be a sha256 digest")
    else:
        workspace["path"] = _safe_path(workspace.get("path"), "workspace.path")

    task["visible_tests"] = _path_list(task["visible_tests"], "visible_tests")
    task["hidden_tests"] = _path_list(task["hidden_tests"], "hidden_tests", allow_empty=True)
    spec = ExecutableSpec.from_value(task["verifier"])
    task["verifier"] = {
        "argv": list(spec.argv),
        "cwd": spec.cwd,
        "clean_env": spec.clean_env,
        "timeout_s": spec.timeout_s,
        "network": spec.network,
    }

    if not isinstance(task["allowed_tools"], list) or not task["allowed_tools"]:
        raise ValueError("allowed_tools must be a non-empty list")
    unknown_tools = sorted(set(task["allowed_tools"]) - set(legal_tool_names()))
    if unknown_tools:
        raise ValueError(f"allowed_tools contains unknown tools: {', '.join(unknown_tools)}")
    budgets = task["budgets"]
    if not isinstance(budgets, dict) or int(budgets.get("step_budget", 0) or 0) <= 0:
        raise ValueError("budgets.step_budget must be positive")
    if task["difficulty"] not in DIFFICULTIES:
        raise ValueError("difficulty is invalid")
    if task["human_time_bucket"] not in HUMAN_TIME_BUCKETS:
        raise ValueError("human_time_bucket is invalid")
    provenance = task["provenance"]
    if not isinstance(provenance, dict) or not str(provenance.get("mined_from_commit", "")).strip():
        raise ValueError("provenance.mined_from_commit is required")
    if task.get("status", "draft") not in {"draft", "active", "quarantine"}:
        raise ValueError("status must be draft, active, or quarantine")
    task.setdefault("status", "draft")
    return task


def _json_paths(paths):
    for value in paths:
        path = Path(value)
        if path.is_dir():
            yield from sorted(path.rglob("*.json"))
        else:
            yield path


def lint_task_paths(paths):
    errors = []
    for path in _json_paths(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            tasks = payload.get("tasks", []) if isinstance(payload, dict) and "tasks" in payload else [payload]
            if not tasks:
                raise ValueError("task collection is empty")
            for task in tasks:
                validate_task(task)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    return errors
