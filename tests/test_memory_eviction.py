import json

from moss.features.memory import LayeredMemory, episodic_note_tokens


def test_value_eviction_keeps_used_early_note_and_respects_token_budget(tmp_path):
    memory = LayeredMemory(
        workspace_root=tmp_path,
        session_id="session-value",
        episodic_token_budget=45,
    )
    memory.append_note(
        "critical deployment uses blue green rollout",
        tags=("deployment",),
        trust="user",
        created_at="2026-08-05T09:00:00+00:00",
    )
    memory.retrieval_candidates("blue green deployment", limit=1)
    memory.retrieval_candidates("blue green deployment", limit=1)
    for index in range(20):
        memory.append_note(
            f"routine transient observation number {index}",
            trust="tool",
            created_at=f"2026-08-05T10:{index:02d}:00+00:00",
        )

    hot = memory.to_dict()["episodic_notes"]

    assert any("blue green" in note["text"] for note in hot)
    assert sum(episodic_note_tokens(note) for note in hot) <= 45
    cold_path = tmp_path / ".moss" / "memory" / "episodic" / "session-value.jsonl"
    assert cold_path.is_file()
    assert len(cold_path.read_text(encoding="utf-8").splitlines()) > 0
    assert not list(cold_path.parent.glob("*.tmp"))


def test_cold_memory_is_explicitly_searchable_but_not_auto_recalled(tmp_path):
    memory = LayeredMemory(
        workspace_root=tmp_path,
        session_id="session-cold",
        episodic_token_budget=18,
    )
    memory.append_note(
        "legacy frobnicator requires copper mode",
        trust="tool",
        created_at="2020-01-01T00:00:00+00:00",
    )
    for index in range(8):
        memory.append_note(
            f"current useful build detail {index}",
            trust="user",
            created_at=f"2026-08-05T11:0{index}:00+00:00",
        )

    assert all("frobnicator" not in note["text"] for note in memory.to_dict()["episodic_notes"])
    assert memory.retrieval_candidates("frobnicator copper", limit=5) == []
    explicit = memory.search("frobnicator copper", limit=5)

    assert explicit[0]["text"] == "legacy frobnicator requires copper mode"
    assert explicit[0]["kind"] == "cold_episodic"


def test_cold_store_round_trips_complete_notes(tmp_path):
    memory = LayeredMemory(
        workspace_root=tmp_path,
        session_id="session-roundtrip",
        episodic_token_budget=10,
    )
    memory.append_note("first old note with metadata", tags=("old",), source="README.md", trust="tool")
    memory.append_note("second newer note that fills budget", tags=("new",), trust="user")

    cold = memory.durable_store.store.load_cold_notes()

    assert cold
    assert {"text", "tags", "source", "created_at", "note_index", "trust"} <= set(cold[0])
    raw = json.loads(
        (tmp_path / ".moss" / "memory" / "episodic" / "session-roundtrip.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert raw == cold[0]
