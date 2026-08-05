import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from moss.evaluation.metrics import (
    _provider_profile,
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
)


def test_evaluation_script_help_entrypoints_import_from_real_package_path():
    repo_root = Path(__file__).resolve().parents[1]
    scripts = [
        "scripts/collect_resume_metrics.py",
        "scripts/run_provider_experiments.py",
        "scripts/run_large_scale_experiments.py",
    ]

    for script in scripts:
        result = subprocess.run(
            [sys.executable, str(repo_root / script), "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout.lower()


def test_run_context_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-ablation-v2.json"

    artifact = run_context_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "context-ablation-v2"
    assert artifact["config_count"] == 12
    assert len(artifact["configs"]) == 12
    assert "current_request_preserved_rate" in artifact["summary"]


def test_provider_profile_loads_project_env_before_reading_deepseek_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "MOSS_DEEPSEEK_API_KEY=sk-project-deepseek",
                "MOSS_DEEPSEEK_MODEL=deepseek-v4-flash",
                "MOSS_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_KEY": "sk-legacy-deepseek",
            "DEEPSEEK_MODEL": "legacy-deepseek-model",
            "DEEPSEEK_API_BASE": "https://legacy.deepseek.example/anthropic",
        },
        clear=True,
    ):
        profile = _provider_profile("deepseek")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-project-deepseek"
    assert profile["model"] == "deepseek-v4-flash"
    assert profile["base_url"] == "https://api.deepseek.com/anthropic"


def test_provider_profile_for_gpt_requires_its_own_key(tmp_path, monkeypatch):
    # 不再跨 provider 回落：只有 Anthropic 的 key 时 gpt 必须是 blocked，
    # 否则会带着注定 401 的 key 去请求官方 api.openai.com。
    monkeypatch.chdir(tmp_path)

    with patch.dict(os.environ, {"MOSS_ANTHROPIC_API_KEY": "sk-anthropic"}, clear=True):
        blocked = _provider_profile("gpt")

    assert blocked["status"] == "blocked"

    with patch.dict(os.environ, {"MOSS_OPENAI_API_KEY": "sk-openai"}, clear=True):
        profile = _provider_profile("gpt")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-openai"
    assert profile["model"] == "gpt-5-5"
    assert profile["base_url"] == "https://api.openai.com/v1"


def test_run_memory_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "memory-ablation-v2.json"

    artifact = run_memory_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "memory-ablation-v2"
    assert artifact["task_count"] == 12
    assert set(artifact["variants"]) == {"memory_on", "memory_off", "memory_irrelevant"}
    assert "memory_hit_rate" in artifact["variants"]["memory_on"]


def test_run_recovery_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "recovery-ablation-v2.json"

    artifact = run_recovery_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "recovery-ablation-v2"
    assert artifact["task_count"] == 10
    assert set(artifact["variants"]) == {"resume_enabled", "resume_disabled"}
    assert set(artifact["variants"]["resume_enabled"]["summary"]) >= {
        "resume_success_rate",
        "stale_reanchor_rate",
        "workspace_drift_detection_rate",
        "resume_false_accept_rate",
    }


def test_write_benchmark_core_report_marks_resume_safe_metrics(tmp_path):
    run_context_ablation_v2(tmp_path / "artifacts" / "context-ablation-v2.json", repetitions=1)
    run_memory_ablation_v2(tmp_path / "artifacts" / "memory-ablation-v2.json", repetitions=1)
    run_recovery_ablation_v2(tmp_path / "artifacts" / "recovery-ablation-v2.json", repetitions=1)
    harness_artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"
    harness_artifact_path.write_text(
        '{"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"failure_category_counts":{}}',
        encoding="utf-8",
    )

    report_path = tmp_path / "docs" / "metrics" / "moss-benchmark-core-report.md"
    report_text = write_benchmark_core_report(
        report_path=report_path,
        harness_artifact_path=harness_artifact_path,
        context_artifact_path=tmp_path / "artifacts" / "context-ablation-v2.json",
        memory_artifact_path=tmp_path / "artifacts" / "memory-ablation-v2.json",
        recovery_artifact_path=tmp_path / "artifacts" / "recovery-ablation-v2.json",
    )

    assert report_path.exists()
    assert "可以安全写进简历的指标" in report_text
    assert "只适合放文档/面试展开的指标" in report_text
    assert "resume_success_rate" in report_text
    assert "memory_hit_rate" in report_text
