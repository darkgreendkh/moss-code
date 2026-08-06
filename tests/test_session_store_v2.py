"""session v2 目录布局与 v1 迁移（spec-07 §4.1）。

守两件事：迁移不丢数据、增量写真的把 O(n²) 降到了 O(n)。
"""

import json

import pytest

from moss import atomic_io
from moss.runs.session import (
    CHECKPOINT_FILE_LIMIT,
    LEGACY_BACKUP_SUFFIX,
    SESSION_SCHEMA_VERSION,
    SessionStore,
)


def _session(session_id="s1", turns=0):
    return {
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "history": [{"role": "user", "content": f"turn {index}"} for index in range(turns)],
        "memory": {"working": {"recent_files": []}},
        "checkpoints": {"current_id": "", "items": {}},
    }


def test_v2_layout_splits_meta_history_and_checkpoints(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=3)
    session["checkpoints"] = {
        "current_id": "ckpt_1",
        "items": {"ckpt_1": {"checkpoint_id": "ckpt_1", "summary": "x"}},
    }

    directory = store.save(session)

    assert sorted(item.name for item in directory.iterdir()) == [
        "checkpoints.jsonl",
        "history.jsonl",
        "meta.json",
    ]
    meta = json.loads(store.meta_path("s1").read_text(encoding="utf-8"))
    assert meta["schema_version"] == SESSION_SCHEMA_VERSION
    assert meta["current_checkpoint_id"] == "ckpt_1"
    # meta 里不该再有整份 history —— 那正是 O(n²) 的来源。
    assert "history" not in meta
    assert len(store.history_path("s1").read_text(encoding="utf-8").splitlines()) == 3


def test_save_then_load_round_trips(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=4)
    session["checkpoints"] = {
        "current_id": "ckpt_2",
        "items": {
            "ckpt_1": {"checkpoint_id": "ckpt_1", "summary": "a"},
            "ckpt_2": {"checkpoint_id": "ckpt_2", "summary": "b"},
        },
    }
    store.save(session)

    loaded = SessionStore(tmp_path / "sessions").load("s1")

    assert loaded == session


def test_appends_are_incremental_not_full_rewrites(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=1)
    store.save(session)
    rewrites = []
    original = store._rewrite_jsonl
    store._rewrite_jsonl = staticmethod(
        lambda path, entries: rewrites.append(path.name) or original(path, entries)
    )

    for index in range(1, 20):
        session["history"].append({"role": "user", "content": f"more {index}"})
        store.save(session)

    assert rewrites == []
    assert len(store.history_path("s1").read_text(encoding="utf-8").splitlines()) == 20


def test_truncated_history_forces_a_rewrite(tmp_path):
    """/reset、rewind、compaction 会改写历史 —— 那不是追加，只能重写。"""
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=5)
    store.save(session)

    session["history"] = session["history"][:2]
    store.save(session)

    assert store.load("s1")["history"] == session["history"]


def test_rewritten_prefix_forces_a_rewrite(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=4)
    store.save(session)

    # 长度不变但内容变了（compaction 把一段历史换成一条摘要）。
    session["history"][3] = {"role": "system", "content": "compacted handoff"}
    store.save(session)

    assert store.load("s1")["history"][3]["content"] == "compacted handoff"


def test_write_volume_drops_by_at_least_95_percent_over_500_turns(tmp_path):
    """验收门槛（spec-07 §7）：500 轮会话累计写入量下降 ≥95%。"""
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=0)
    written = []

    real_write_atomic = atomic_io.write_atomic
    real_append_line = atomic_io.append_line

    def counting_write(path, data, **kwargs):
        written.append(len(str(data)))
        return real_write_atomic(path, data, **kwargs)

    def counting_append(path, line, **kwargs):
        written.append(len(str(line)))
        return real_append_line(path, line, **kwargs)

    import moss.runs.session as module

    module.write_atomic = counting_write
    module.append_line = counting_append
    try:
        v1_total = 0
        for index in range(500):
            session["history"].append({"role": "user", "content": f"turn {index}"})
            # v1 的口径：每次 record 把整份 session 重新序列化落盘。
            v1_total += len(json.dumps(session, indent=2, ensure_ascii=False))
            store.save(session)
    finally:
        module.write_atomic = real_write_atomic
        module.append_line = real_append_line

    v2_total = sum(written)
    reduction = 1 - (v2_total / v1_total)
    assert reduction >= 0.95, f"only {reduction:.1%} (v1={v1_total}, v2={v2_total})"


def test_v1_single_file_migrates_to_v2_and_keeps_a_backup(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=3)
    session["checkpoints"] = {
        "current_id": "ckpt_1",
        "items": {"ckpt_1": {"checkpoint_id": "ckpt_1", "summary": "x"}},
    }
    store.legacy_path("s1").write_text(json.dumps(session), encoding="utf-8")

    loaded = store.load("s1")

    assert loaded["history"] == session["history"]
    assert loaded["checkpoints"] == session["checkpoints"]
    assert not store.legacy_path("s1").exists()
    backup = tmp_path / "sessions" / ("s1.json" + LEGACY_BACKUP_SUFFIX)
    assert json.loads(backup.read_text(encoding="utf-8"))["history"] == session["history"]


def test_migration_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=3)
    store.legacy_path("s1").write_text(json.dumps(session), encoding="utf-8")

    first = store.load("s1")
    second = store.load("s1")
    third = SessionStore(tmp_path / "sessions").load("s1")

    assert first == second == third
    assert len(store.history_path("s1").read_text(encoding="utf-8").splitlines()) == 3


def test_migration_refuses_to_lose_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = _session(turns=5)
    store.legacy_path("s1").write_text(json.dumps(session), encoding="utf-8")
    # 模拟写 history 时少写了几条：迁移必须炸，而不是安静地丢掉用户的会话。
    monkeypatch.setattr(
        SessionStore, "_rewrite_jsonl", staticmethod(lambda path, entries: path.write_text("", encoding="utf-8"))
    )

    with pytest.raises(RuntimeError, match="lost history"):
        store.load("s1")

    # 原件必须还在 —— 迁移失败时它是唯一的数据来源。
    assert store.legacy_path("s1").exists()


def test_stray_v1_file_next_to_v2_is_archived_not_replayed(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(_session(turns=2))
    store.legacy_path("s1").write_text(json.dumps(_session(turns=99)), encoding="utf-8")

    loaded = store.load("s1")

    # v2 已经是权威版本，旧文件只归档，不能反过来覆盖它。
    assert len(loaded["history"]) == 2
    assert (tmp_path / "sessions" / ("s1.json" + LEGACY_BACKUP_SUFFIX)).exists()


def test_latest_sees_both_layouts(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.legacy_path("old_one").write_text(json.dumps(_session("old_one")), encoding="utf-8")
    store.save(_session("new_one"))

    assert store.latest() == "new_one"


def test_latest_prefers_the_most_recently_written_session(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(_session("first"))
    store.save(_session("second"))
    store.save(_session("first", turns=1))

    assert store.latest() == "first"


def test_checkpoints_file_is_compacted_when_it_grows(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = _session()
    items = session["checkpoints"]["items"]

    for index in range(CHECKPOINT_FILE_LIMIT + 5):
        checkpoint_id = f"ckpt_{index}"
        items[checkpoint_id] = {"checkpoint_id": checkpoint_id, "summary": str(index)}
        session["checkpoints"]["current_id"] = checkpoint_id
        # runtime 侧只保留最近 40 条；store 照着内存里那份紧凑化。
        if len(items) > 40:
            for stale in list(items)[:-40]:
                items.pop(stale)
        store.save(session)

    lines = store.checkpoints_path("s1").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= CHECKPOINT_FILE_LIMIT
    assert store.load("s1")["checkpoints"]["current_id"] == f"ckpt_{CHECKPOINT_FILE_LIMIT + 4}"


def test_partial_trailing_history_line_is_skipped_on_load(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(_session(turns=2))
    with store.history_path("s1").open("a", encoding="utf-8") as handle:
        handle.write('{"role": "user"')

    assert len(store.load("s1")["history"]) == 2


def test_load_of_a_missing_session_raises(tmp_path):
    store = SessionStore(tmp_path / "sessions")

    with pytest.raises(FileNotFoundError):
        store.load("nope")
