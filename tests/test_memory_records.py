import json
from dataclasses import replace

import pytest

from moss.features import memory_store as memory_store_module
from moss.features.memory_records import MemoryRecord, SourceRef, make_record
from moss.features.memory_store import MemoryStore


def _record(text="Use pytest", **overrides):
    fields = {
        "scope": "project",
        "scope_key": "repo-1",
        "topic": "project-conventions",
        "subject": "test runner",
        "text": text,
        "tags": ("test",),
        "trust": "user",
        "source_refs": (SourceRef(run_id="run-1", event_seq=3, path="README.md"),),
        "created_at": "2026-08-05T10:00:00+00:00",
        "observed_at": "2026-08-05T09:59:00+00:00",
    }
    fields.update(overrides)
    return make_record(**fields)


def test_memory_record_round_trips_complete_schema():
    record = _record()

    restored = MemoryRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.schema_version == 1
    assert restored.id.startswith("mem_")
    assert len(restored.id) == 16
    assert restored.source_refs[0].event_seq == 3


def test_store_keeps_logical_append_history_and_folds_latest_id(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    record = _record()

    store.append(record)
    store.append(replace(record, hit_count=4))

    lines = store.records_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["hit_count"] for line in lines] == [0, 4]
    assert store.all_records() == [replace(record, hit_count=4)]


def test_update_appends_new_record_and_supersedes_old_record(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    old = _record("Use pytest 8")
    store.append(old)

    new = store.update(old.id, "Use pytest 9", observed_at="2026-08-05T11:00:00+00:00")

    assert new.id != old.id
    assert new.supersedes == (old.id,)
    assert store.active_records() == [new]
    folded = {record.id: record for record in store.all_records()}
    assert folded[old.id].status == "superseded"
    assert folded[new.id].status == "active"
    assert len(store.records_path.read_text(encoding="utf-8").splitlines()) == 3


def test_tombstone_survives_reload_and_cannot_reappear_in_active_records(tmp_path):
    root = tmp_path / "memory"
    store = MemoryStore(root)
    record = _record()
    store.append(record)
    store.delete(record.id)

    reloaded = MemoryStore(root)

    assert reloaded.active_records() == []
    assert reloaded.get(record.id).status == "tombstone"
    reloaded.rebuild_projections()
    assert "Use pytest" not in reloaded.index_path.read_text(encoding="utf-8")
    assert not (reloaded.topics_dir / "project-conventions.md").exists()


def test_compaction_preserves_folded_records_and_projection(tmp_path):
    store = MemoryStore(tmp_path / "memory", compact_threshold=3)
    first = _record("Use pytest")
    second = _record("Use ruff", subject="linter")
    store.append(first)
    store.append(second)
    store.rebuild_projections()
    before = store.index_path.read_text(encoding="utf-8")

    store.append(replace(first, hit_count=1))
    store.append(replace(first, hit_count=2))
    store.rebuild_projections()

    assert len(store.records_path.read_text(encoding="utf-8").splitlines()) == 2
    assert store.index_path.read_text(encoding="utf-8") == before
    assert {record.text: record.hit_count for record in store.active_records()} == {
        "Use pytest": 2,
        "Use ruff": 0,
    }


def test_trailing_partial_json_does_not_destroy_last_complete_generation(tmp_path):
    root = tmp_path / "memory"
    store = MemoryStore(root)
    record = _record()
    store.append(record)
    with store.records_path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1,"id":"half')

    recovered = MemoryStore(root)

    assert recovered.active_records() == [record]
    recovered.append(replace(record, used_count=2))
    assert recovered.active_records() == [replace(record, used_count=2)]
    assert not list(root.glob("records.jsonl.*.tmp"))


def test_failed_atomic_replace_leaves_previous_generation_loadable(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    store = MemoryStore(root)
    record = _record()
    store.append(record)
    real_replace = memory_store_module.os.replace

    def fail_records_replace(source, destination):
        if destination == store.records_path:
            raise OSError("simulated process death before replace")
        return real_replace(source, destination)

    monkeypatch.setattr(memory_store_module.os, "replace", fail_records_replace)

    with pytest.raises(OSError, match="simulated process death"):
        store.append(replace(record, hit_count=9))

    assert MemoryStore(root).active_records() == [record]
    assert not list(root.glob("records.jsonl.*.tmp"))
