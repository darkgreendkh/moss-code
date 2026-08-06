import json
from pathlib import Path

import pytest

from moss.evaluation.evaluator import BenchmarkEvaluator, _assert_scratch_workspace, summarize_rows
from moss.evaluation.metrics import (
    _provider_summary_from_artifact,
    _runtime_facts,
    run_context_ablation_v2,
)
from moss.providers.capabilities import PROVIDER_CAPABILITIES
from moss.tools import legal_tool_names


def test_evaluation_workspace_guard_rejects_real_checkout_and_accepts_pytest_scratch(tmp_path):
    with pytest.raises(ValueError, match="scratch workspace"):
        _assert_scratch_workspace(Path.cwd())

    assert _assert_scratch_workspace(tmp_path) == tmp_path.resolve()
    assert _assert_scratch_workspace(Path.cwd(), allow_dirty=True) == Path.cwd().resolve()


def test_benchmark_evaluator_requires_explicit_override_for_real_checkout():
    with pytest.raises(ValueError, match="scratch workspace"):
        BenchmarkEvaluator(workspace_root=Path.cwd())

    evaluator = BenchmarkEvaluator(workspace_root=Path.cwd(), allow_dirty_workspace=True)
    assert evaluator.workspace_root == Path.cwd()


def test_summarize_rows_uses_status_as_the_only_pass_fail_contract():
    summary = summarize_rows(
        [
            {"status": "fail", "passed": True},
            {"status": "pass", "passed": False},
        ]
    )

    assert summary["passed"] == 1
    assert summary["failed"] == 1


def test_ablation_artifact_uses_shared_utc_clock(tmp_path, monkeypatch):
    monkeypatch.setattr("moss.evaluation.metrics.now", lambda: "2030-01-02T03:04:05+00:00")

    with pytest.warns(DeprecationWarning, match="L1 compatibility"):
        artifact = run_context_ablation_v2(tmp_path / "context.json", repetitions=1)

    assert artifact["captured_at"] == "2030-01-02T03:04:05+00:00"


def test_runtime_facts_follow_live_tool_and_provider_registries(tmp_path):
    facts = _runtime_facts(tmp_path)

    assert facts == {
        "model_backend_count": len({provider for provider, _prefix in PROVIDER_CAPABILITIES}),
        "tool_count": len(legal_tool_names()),
        "run_artifact_count": 3,
    }


def test_provider_summary_takes_artifact_path_out_of_band():
    payload = {
        "summary": {"total_tasks": 1, "pass_rate": 1.0},
        "rows": [],
        "_artifact_path": "must-not-leak.json",
    }

    summary = _provider_summary_from_artifact(payload, artifact_path="public/provider.json")

    assert summary["artifact_path"] == "public/provider.json"
    assert not any(key.startswith("_") for key in summary)


def test_archived_json_artifacts_declare_that_the_snapshot_is_historical():
    archive = Path("benchmarks/results/main-resume-repro-2026-06-07")
    for path in archive.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["historical_snapshot_note"] == "历史合成快照，不可在当前 checkout 复现"
