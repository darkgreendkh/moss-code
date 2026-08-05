from moss.features.memory import LayeredMemory
from moss.features.memory_store import MemoryStore


def _write_legacy_memory(root):
    topics = root / "topics"
    topics.mkdir(parents=True)
    (root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention, project\n",
        encoding="utf-8",
    )
    (topics / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention, project\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n"
        "- Preserve local state under .moss/.\n",
        encoding="utf-8",
    )


def test_legacy_markdown_migration_is_idempotent_and_keeps_backup(tmp_path):
    memory_root = tmp_path / ".moss" / "memory"
    _write_legacy_memory(memory_root)
    original_index = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
    store = MemoryStore(memory_root, workspace_root=tmp_path)

    first = store.migrate_legacy()
    first_lines = store.records_path.read_text(encoding="utf-8").splitlines()
    second = store.migrate_legacy()

    assert first == 2
    assert second == 0
    assert len(first_lines) == 2
    assert store.records_path.read_text(encoding="utf-8").splitlines() == first_lines
    assert (memory_root / "MEMORY.md.bak").read_text(encoding="utf-8") == original_index
    assert all(record.legacy for record in store.active_records())
    assert all(record.trust == "user" for record in store.active_records())
    assert all(record.source_refs == () for record in store.active_records())


def test_migration_rebuilds_semantically_equivalent_markdown_projection(tmp_path):
    memory_root = tmp_path / ".moss" / "memory"
    _write_legacy_memory(memory_root)
    store = MemoryStore(memory_root, workspace_root=tmp_path)

    store.migrate_legacy()

    projected_index = store.index_path.read_text(encoding="utf-8")
    projected_topic = (store.topics_dir / "project-conventions.md").read_text(encoding="utf-8")
    assert "project-conventions" in projected_index
    assert "convention, project" in projected_index
    assert "Use constrained tools instead of guessing." in projected_topic
    assert "Preserve local state under .moss/." in projected_topic


def test_layered_memory_uses_records_as_durable_source_of_truth(tmp_path):
    memory_root = tmp_path / ".moss" / "memory"
    _write_legacy_memory(memory_root)

    memory = LayeredMemory(workspace_root=tmp_path)

    assert (memory_root / "records.jsonl").exists()
    assert memory.to_dict()["durable_topics"] == ["project-conventions"]
    hits = memory.retrieval_candidates("constrained tools", limit=2)
    assert [hit["text"] for hit in hits] == ["Use constrained tools instead of guessing."]


def test_memory_v2_can_fall_back_to_legacy_markdown_for_one_release(tmp_path, monkeypatch):
    memory_root = tmp_path / ".moss" / "memory"
    _write_legacy_memory(memory_root)
    monkeypatch.setenv("MOSS_MEMORY_V2", "off")

    memory = LayeredMemory(workspace_root=tmp_path)

    assert not (memory_root / "records.jsonl").exists()
    hits = memory.retrieval_candidates("constrained tools", limit=2)
    assert [hit["text"] for hit in hits] == ["Use constrained tools instead of guessing."]
