"""Convert caller-supplied SWE-style records into Moss task schema v2.

This module deliberately does not download a dataset or repository. The caller must
provide a locally prepared archive digest and test paths, keeping licensing and data
access decisions outside Moss.
"""

import hashlib
import json
from pathlib import Path

from ...clock import now
from ..task_schema import validate_task


_NEGATIVE_PATCHES = [
    {"name": "delete-key-assertion", "operator": "delete_assertion"},
    {"name": "surface-text-only", "operator": "surface_text"},
    {"name": "obvious-wrong", "operator": "obvious_wrong"},
]


def _required(record, key):
    value = str(record.get(key) or "").strip()
    if not value:
        raise ValueError(f"SWE-style record requires {key}")
    return value


def adapt_swe_task(
    record,
    *,
    archive_sha256,
    overlay_paths,
    visible_tests,
    hidden_tests=None,
    split,
    license_name,
    source_url,
    dataset_release,
    mined_at=None,
    verifier_argv=None,
):
    record = dict(record or {})
    instance_id = _required(record, "instance_id")
    repository = _required(record, "repo")
    base_commit = _required(record, "base_commit")
    prompt = _required(record, "problem_statement")
    license_name = str(license_name or "").strip()
    source_url = str(source_url or "").strip()
    if not license_name:
        raise ValueError("public benchmark adapter requires an explicit license")
    if not source_url:
        raise ValueError("public benchmark adapter requires a source URL")
    visible_tests = list(visible_tests or ())
    hidden_tests = list(hidden_tests or ())
    overlay_paths = list(overlay_paths or ())
    if not visible_tests or not overlay_paths:
        raise ValueError("public benchmark adapter requires local visible tests and overlays")
    seed = int(hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:8], 16)
    task = {
        "schema_version": 2,
        "task_id": "public-" + instance_id.replace("/", "__"),
        "suite": "coding-mined",
        "eval_level": "L2",
        "prompt": prompt,
        "raw_prompt": prompt,
        "workspace": {
            "kind": "git_archive",
            "base_commit": base_commit,
            "overlay_paths": overlay_paths,
            "archive_sha256": str(archive_sha256),
        },
        "visible_tests": visible_tests,
        "hidden_tests": hidden_tests,
        "holdout_seed": seed,
        "holdout_status": "held_out" if hidden_tests else "no_holdout",
        "verifier": {
            "argv": list(verifier_argv or ["python", "-m", "pytest", "-q", *visible_tests]),
            "cwd": ".",
            "clean_env": True,
            "timeout_s": 300,
            "network": "deny",
        },
        "allowed_tools": ["list_files", "read_file", "edit_file", "run_shell"],
        "budgets": {"step_budget": 25, "max_usd": 0.5, "max_seconds": 900},
        "difficulty": str(record.get("difficulty") or "needs_iteration"),
        "human_time_bucket": str(record.get("human_time_bucket") or "hours"),
        "provenance": {
            "mined_from_commit": base_commit,
            "mined_at": str(mined_at or now()),
            "min_model_cutoff": str(dataset_release),
            "contamination_status": "public-known",
            "benchmark": "swe-style",
            "instance_id": instance_id,
            "repository": repository,
            "dataset_version": str(record.get("version") or dataset_release),
            "split": str(split),
            "license": license_name,
            "source_url": source_url,
        },
        "rubric": None,
        "negative_patches": list(_NEGATIVE_PATCHES),
        "status": "draft",
    }
    return validate_task(task)


def load_swe_jsonl(path):
    rows = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid SWE JSONL line {line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"SWE JSONL line {line_number} must be an object")
        rows.append(value)
    return rows
