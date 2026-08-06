from dataclasses import asdict

from moss.evaluation.manifest import RunManifest


def test_run_manifest_contains_every_reproducibility_field():
    manifest = RunManifest.capture(
        repo_root=".",
        prompt_version="p3",
        tool_schema={"read_file": {"path": "string"}},
        policy_version="policy-v2",
        provider="deepseek",
        model="deepseek-v4-pro",
        decoding={"temperature": 0.0},
        taskset=b"tasks",
        fixture=b"fixture",
        split="pilot",
        max_steps=20,
        budgets={"max_usd": 0.5},
        workers=4,
        sandbox="process",
    )

    payload = asdict(manifest)
    assert manifest.schema_version == 1
    assert manifest.workers == 4
    assert manifest.price_table_date
    assert manifest.tool_schema_sha.startswith("sha256:")
    assert manifest.taskset_sha.startswith("sha256:")
    assert manifest.fixture_sha.startswith("sha256:")
    assert manifest.agent_commit
    assert set(payload) == {
        "schema_version",
        "started_at",
        "agent_commit",
        "git_dirty",
        "git_diff_sha",
        "prompt_version",
        "tool_schema_sha",
        "policy_version",
        "provider",
        "model",
        "decoding",
        "taskset_sha",
        "fixture_sha",
        "split",
        "python",
        "os",
        "arch",
        "rg_version",
        "git_version",
        "max_steps",
        "budgets",
        "workers",
        "sandbox",
        "judge_model",
        "judge_prompt_sha",
        "calibration_sha",
        "price_table_date",
    }

