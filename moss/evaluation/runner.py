"""Process-isolated evaluation runner with explicit infrastructure accounting."""

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile
import time

from ..atomic_io import write_json_atomic


def _workspace_name(task_id, attempt):
    digest = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:10]
    return f"{digest}-attempt-{attempt}"


def _execute(executor, task, workspace):
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / ".moss").mkdir()
    return executor(dict(task), str(workspace))


def _infra_row(task, exc):
    return {
        "task_id": str(task.get("task_id") or "unknown"),
        "status": "infra_failure",
        "infra_stage": "child_process",
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


def _summary(rows):
    valid = [row for row in rows if row.get("status") != "infra_failure"]
    passed = sum(1 for row in valid if row.get("status") == "pass")
    total = len(rows)
    return {
        "started_trials": total,
        "valid_trials": len(valid),
        "capability_passes": passed,
        "infra_failures": total - len(valid),
        "capability_rate_valid_environment": passed / len(valid) if valid else 0.0,
        "end_to_end_reliability": passed / total if total else 0.0,
    }


def regression_decision(comparison, *, min_pairs=20, threshold=0.0):
    comparison = dict(comparison or {})
    required = {"n", "estimate", "ci_low", "ci_high"}
    if required - set(comparison):
        raise ValueError("regression comparison requires n, estimate, ci_low, and ci_high")
    if int(comparison["n"]) < int(min_pairs):
        return {"status": "warning", "reason": "insufficient_sample", **comparison}
    if float(comparison["ci_high"]) < -float(threshold):
        return {"status": "fail", "reason": "significant_regression", **comparison}
    return {"status": "pass", "reason": "no_significant_regression", **comparison}


def run_parallel_trials(
    tasks,
    executor,
    *,
    manifest,
    artifact_path,
    rerun_policy=None,
):
    tasks = [dict(task) for task in tasks]
    artifact_path = Path(artifact_path)
    rerun_policy = dict(rerun_policy or {"max_infra_retries": 0, "backoff_s": 0})
    max_retries = int(rerun_policy.get("max_infra_retries", 0))
    backoff_s = float(rerun_policy.get("backoff_s", 0))
    if max_retries < 0:
        raise ValueError("max_infra_retries must be non-negative")
    if not 0 <= backoff_s <= 60:
        raise ValueError("backoff_s must be between zero and 60")
    write_json_atomic(
        artifact_path,
        {
            "schema_version": 1,
            "status": "incomplete",
            "started_trials": len(tasks),
        },
    )

    attempts = {str(task.get("task_id") or "unknown"): [] for task in tasks}
    final_rows = {}
    pending = list(tasks)
    with tempfile.TemporaryDirectory(prefix="moss-eval-run-") as temp_dir:
        root = Path(temp_dir)
        with ProcessPoolExecutor(max_workers=manifest.workers) as pool:
            for attempt in range(max_retries + 1):
                if attempt and pending and backoff_s:
                    time.sleep(backoff_s)
                futures = {}
                for task in pending:
                    task_id = str(task.get("task_id") or "unknown")
                    workspace = root / _workspace_name(task_id, attempt)
                    future = pool.submit(_execute, executor, task, workspace)
                    futures[future] = task
                retry = []
                for future in as_completed(futures):
                    task = futures[future]
                    task_id = str(task.get("task_id") or "unknown")
                    try:
                        row = dict(future.result())
                        row.setdefault("task_id", task_id)
                        if row.get("status") not in {"pass", "fail", "infra_failure"}:
                            raise ValueError("worker status must be pass, fail, or infra_failure")
                    except Exception as exc:
                        row = _infra_row(task, exc)
                    attempts[task_id].append(row)
                    if row["status"] == "infra_failure" and attempt < max_retries:
                        retry.append(task)
                    else:
                        final_rows[task_id] = row
                pending = retry

    rows = [final_rows[str(task.get("task_id") or "unknown")] for task in tasks]
    artifact = {
        "schema_version": 1,
        "status": "complete",
        "manifest": asdict(manifest),
        "rerun_policy": rerun_policy,
        "summary": _summary(rows),
        "rows": rows,
        "attempts": attempts,
    }
    write_json_atomic(artifact_path, artifact)
    return artifact
