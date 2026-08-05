import hashlib

from moss.features.memory import LayeredMemory
from moss.features.memory_records import SourceRef


def _write(memory, text, *, trust="model", observed_at="2026-08-05T10:00:00+00:00", path=None, sha=None):
    refs = (SourceRef(run_id="run-1", path=path, content_sha=sha),)
    return memory.write_durable(
        scope="project",
        topic="dependency-facts",
        text=text,
        trust=trust,
        source_refs=refs,
        observed_at=observed_at,
    )[0]


def test_aliases_normalize_subject_before_newer_fact_supersedes(tmp_path):
    memory_root = tmp_path / ".moss" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "aliases.md").write_text(
        "default provider = provider default = provider 默认值\n",
        encoding="utf-8",
    )
    memory = LayeredMemory(workspace_root=tmp_path)

    old = _write(memory, "default provider is OpenAI", observed_at="2026-08-05T10:00:00+00:00")
    new = _write(memory, "provider 默认值 is Anthropic", observed_at="2026-08-05T12:00:00+00:00")

    assert old.subject == new.subject == "default provider"
    assert memory.durable_store.store.active_records() == [new]
    assert memory.durable_store.store.get(old.id).status == "superseded"
    assert new.supersedes == (old.id,)


def test_higher_trust_wins_even_when_observed_earlier(tmp_path):
    memory = LayeredMemory(workspace_root=tmp_path)

    old = _write(memory, "test runner is nose", trust="model", observed_at="2026-08-05T12:00:00+00:00")
    new = _write(memory, "test runner is pytest", trust="user", observed_at="2026-08-05T11:00:00+00:00")

    assert memory.durable_store.store.active_records() == [new]
    assert memory.durable_store.store.get(old.id).status == "superseded"


def test_close_same_trust_contradiction_marks_both_for_review_and_recall(tmp_path):
    memory = LayeredMemory(workspace_root=tmp_path)

    first = _write(memory, "network access is allowed", observed_at="2026-08-05T10:00:00+00:00")
    second = _write(memory, "network access is not allowed", observed_at="2026-08-05T10:30:00+00:00")

    store = memory.durable_store.store
    assert store.active_records() == []
    assert {record.id for record in store.recallable_records()} == {first.id, second.id}
    assert {record.status for record in store.recallable_records()} == {"needs_review"}
    view = memory.retrieval_view("network access allowed", limit=5)
    assert first.text in view
    assert second.text in view
    assert "存在冲突" in view


def test_stale_source_hash_marks_record_for_review_before_recall(tmp_path):
    source = tmp_path / "pyproject.toml"
    source.write_text("requires-python = '>=3.12'\n", encoding="utf-8")
    content_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    memory = LayeredMemory(workspace_root=tmp_path)
    record = _write(
        memory,
        "Python version is 3.12",
        path="pyproject.toml",
        sha=content_sha,
    )
    source.write_text("requires-python = '>=3.13'\n", encoding="utf-8")

    view = memory.retrieval_view("Python version", limit=5)

    assert memory.durable_store.store.get(record.id).status == "needs_review"
    assert "可能已过期" in view
    assert record.text in view


def test_matching_source_hash_remains_active(tmp_path):
    source = tmp_path / "README.md"
    source.write_text("Use uv run pytest.\n", encoding="utf-8")
    content_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    memory = LayeredMemory(workspace_root=tmp_path)
    record = _write(memory, "test runner is pytest", path="README.md", sha=content_sha)

    memory.retrieval_candidates("pytest", limit=5)

    assert memory.durable_store.store.get(record.id).status == "active"
