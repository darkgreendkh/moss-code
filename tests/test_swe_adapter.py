import json
from pathlib import Path

import pytest

from moss.evaluation.adapters import get_adapter
from moss.evaluation.adapters.swe_task import adapt_swe_task, load_swe_jsonl
from moss.evaluation.task_schema import validate_task


def _record():
    return {
        "instance_id": "owner__repo-123",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "Fix the parser without changing public behavior.",
        "patch": "SECRET ANSWER PATCH MUST NOT LEAK",
        "test_patch": "SECRET TEST PATCH MUST NOT LEAK",
        "version": "v1.2",
    }


def test_swe_adapter_produces_valid_draft_without_leaking_answer_patches():
    task = adapt_swe_task(
        _record(),
        archive_sha256="sha256:" + "b" * 64,
        overlay_paths=["tests/test_parser.py"],
        visible_tests=["tests/test_parser.py::test_public"],
        hidden_tests=["tests/test_parser.py::test_edge"],
        split="test",
        license_name="MIT",
        source_url="https://example.invalid/dataset",
        dataset_release="2026-01",
        mined_at="2026-08-06T00:00:00+00:00",
    )

    assert validate_task(task) == task
    assert task["status"] == "draft"
    assert task["eval_level"] == "L2"
    assert task["provenance"]["benchmark"] == "swe-style"
    assert task["provenance"]["license"] == "MIT"
    assert task["provenance"]["split"] == "test"
    assert "SECRET ANSWER" not in json.dumps(task)
    assert "SECRET TEST" not in json.dumps(task)


def test_swe_adapter_requires_explicit_license_and_local_archive():
    with pytest.raises(ValueError, match="license"):
        adapt_swe_task(
            _record(),
            archive_sha256="sha256:" + "b" * 64,
            overlay_paths=["tests/test_parser.py"],
            visible_tests=["tests/test_parser.py"],
            split="test",
            license_name="",
            source_url="https://example.invalid/dataset",
            dataset_release="2026-01",
        )


def test_jsonl_loader_is_dependency_free_and_registry_resolves_adapter(tmp_path):
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps(_record()) + "\n", encoding="utf-8")

    assert load_swe_jsonl(path) == [_record()]
    assert get_adapter("swe-style") is adapt_swe_task


def test_documentation_marks_live_evidence_and_manual_gold_as_pending():
    readme = Path("README.md").read_text(encoding="utf-8")
    spec = Path("docs/specs/spec-08-evaluation.md").read_text(encoding="utf-8")
    guide = Path("docs/features/evaluation.md").read_text(encoding="utf-8")

    assert "L0–L4" in readme
    assert "Implemented（框架完成）" in spec
    assert "L2/L3 真实模型结果：待运行" in spec
    assert "50 条人工盲标：待标注" in spec
    assert "eval_audit_tasks.py" in guide
    assert "不能声称模型能力" in guide
