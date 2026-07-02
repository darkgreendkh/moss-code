import json

from moss.session_store import SessionStore


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".moss" / "sessions")
    first = {"id": "session_001", "history": [{"role": "user", "content": "first"}]}
    second = {"id": "session_002", "history": [{"role": "user", "content": "second"}]}

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(first_path.read_text(encoding="utf-8"))["id"] == "session_001"
    assert store.load("session_002") == second
    assert store.latest() == second_path.stem


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".moss" / "sessions")

    assert store.latest() is None


def test_session_store_save_leaves_no_partial_or_temp_files(tmp_path):
    store = SessionStore(tmp_path / ".moss" / "sessions")
    session = {"id": "session_atomic", "history": [{"role": "user", "content": "x"}]}

    store.save(session)
    # 覆盖写第二次，确认没有留下 .tmp 残留，最终文件仍是完整可解析 JSON。
    session["history"].append({"role": "assistant", "content": "y"})
    path = store.save(session)

    files = sorted(p.name for p in store.root.iterdir())
    assert files == ["session_atomic.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == session


def test_session_store_latest_ignores_delegate_sessions(tmp_path):
    # 委派会话写在独立目录，不能污染用户 sessions 目录的 latest()。
    sessions = SessionStore(tmp_path / ".moss" / "sessions")
    delegates = SessionStore(tmp_path / ".moss" / "delegates")

    sessions.save({"id": "user_session", "history": []})
    delegates.save({"id": "delegate_session", "history": []})

    assert sessions.latest() == "user_session"
