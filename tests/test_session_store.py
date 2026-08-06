import json

from moss.runs.session import SessionStore


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".moss" / "sessions")
    first = {"id": "session_001", "history": [{"role": "user", "content": "first"}]}
    second = {"id": "session_002", "history": [{"role": "user", "content": "second"}]}

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(store.meta_path("session_001").read_text(encoding="utf-8"))["id"] == "session_001"
    assert store.load("session_002")["history"] == second["history"]
    assert store.latest() == second_path.name


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".moss" / "sessions")

    assert store.latest() is None


def test_session_store_save_leaves_no_partial_or_temp_files(tmp_path):
    store = SessionStore(tmp_path / ".moss" / "sessions")
    session = {"id": "session_atomic", "history": [{"role": "user", "content": "x"}]}

    store.save(session)
    # 覆盖写第二次，确认没有留下 .tmp 残留，读回来的仍是完整会话。
    session["history"].append({"role": "assistant", "content": "y"})
    path = store.save(session)

    assert sorted(item.name for item in store.root.iterdir()) == ["session_atomic"]
    assert sorted(item.name for item in path.iterdir()) == ["history.jsonl", "meta.json"]
    assert store.load("session_atomic")["history"] == session["history"]


def test_session_store_latest_ignores_delegate_sessions(tmp_path):
    # 委派会话写在独立目录，不能污染用户 sessions 目录的 latest()。
    sessions = SessionStore(tmp_path / ".moss" / "sessions")
    delegates = SessionStore(tmp_path / ".moss" / "delegates")

    sessions.save({"id": "user_session", "history": []})
    delegates.save({"id": "delegate_session", "history": []})

    assert sessions.latest() == "user_session"
