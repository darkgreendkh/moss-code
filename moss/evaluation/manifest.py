"""Immutable provenance manifest for reproducible evaluation runs."""

from dataclasses import dataclass
import hashlib
import json
import platform
from pathlib import Path
import subprocess

from ..clock import now
from .pricing import PRICE_TABLE_DATE


def _command(root, *argv):
    try:
        return subprocess.run(
            list(argv),
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "not-available"


def _sha(value):
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    started_at: str
    agent_commit: str
    git_dirty: bool
    git_diff_sha: str
    prompt_version: str
    tool_schema_sha: str
    policy_version: str
    provider: str
    model: str
    decoding: dict
    taskset_sha: str
    fixture_sha: str
    split: str
    python: str
    os: str
    arch: str
    rg_version: str
    git_version: str
    max_steps: int
    budgets: dict
    workers: int
    sandbox: str
    judge_model: str | None
    judge_prompt_sha: str | None
    calibration_sha: str | None
    price_table_date: str

    @classmethod
    def capture(
        cls,
        *,
        repo_root,
        prompt_version,
        tool_schema,
        policy_version,
        provider,
        model,
        decoding,
        taskset,
        fixture,
        split,
        max_steps,
        budgets,
        workers,
        sandbox,
        judge_model=None,
        judge_prompt_sha=None,
        calibration_sha=None,
    ):
        root = Path(repo_root).resolve()
        workers = int(workers)
        if workers < 1:
            raise ValueError("workers must be positive")
        status = _command(root, "git", "status", "--porcelain")
        diff = _command(root, "git", "diff", "HEAD", "--binary")
        return cls(
            schema_version=1,
            started_at=now(),
            agent_commit=_command(root, "git", "rev-parse", "HEAD"),
            git_dirty=bool(status and status != "not-available"),
            git_diff_sha=_sha(diff),
            prompt_version=str(prompt_version),
            tool_schema_sha=_sha(tool_schema),
            policy_version=str(policy_version),
            provider=str(provider),
            model=str(model),
            decoding=dict(decoding or {}),
            taskset_sha=_sha(taskset),
            fixture_sha=_sha(fixture),
            split=str(split),
            python=platform.python_version(),
            os=platform.system(),
            arch=platform.machine(),
            rg_version=_command(root, "rg", "--version").splitlines()[0],
            git_version=_command(root, "git", "--version").splitlines()[0],
            max_steps=int(max_steps),
            budgets=dict(budgets or {}),
            workers=workers,
            sandbox=str(sandbox),
            judge_model=None if judge_model is None else str(judge_model),
            judge_prompt_sha=judge_prompt_sha,
            calibration_sha=calibration_sha,
            price_table_date=PRICE_TABLE_DATE,
        )
