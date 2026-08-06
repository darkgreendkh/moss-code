"""trace 序号与哈希链的 O(1) 追加（spec-07 §4.3）。

原来每追加一条事件都要重读整份 trace 算序号和 prev_hash —— n 条事件 O(n²)。
这些测试守的是"追加成本不随已有条数增长"，以及缓存推进不能偏离磁盘事实。
"""

import json
import time

from moss.run_store import TRACE_CHAIN_GENESIS, RunStore, event_digest
from moss.task_state import TaskState


def _store(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = TaskState.create(run_id="run_seq", task_id="t", user_request="x")
    store.start_run(state)
    return store, state


def test_thousand_events_stay_linear(tmp_path):
    store, state = _store(tmp_path)

    started = time.monotonic()
    for index in range(1000):
        store.append_trace(state, {"event": "step", "index": index})
    elapsed_ms = (time.monotonic() - started) * 1000

    events = store.read_trace(state.run_id)
    assert [event["sequence"] for event in events] == list(range(1, 1001))
    # 验收门槛（spec-07 §7）：1000 条 <200ms。留一倍余量抗 CI 抖动。
    assert elapsed_ms < 400, f"1000 events took {elapsed_ms:.0f}ms"


def test_sequence_and_chain_survive_a_fresh_store(tmp_path):
    """新进程接着往同一个 trace 里写：序号必须从磁盘末行接上，而不是从 1 重来。"""
    store, state = _store(tmp_path)
    for index in range(3):
        store.append_trace(state, {"event": "step", "index": index})

    reopened = RunStore(tmp_path / "runs")
    reopened.append_trace(state, {"event": "step", "index": 3})

    ok, problems = reopened.verify_trace(state.run_id)
    events = reopened.read_trace(state.run_id)
    assert ok, problems
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert events[3]["prev_hash"] == event_digest(events[2])


def test_first_event_starts_the_chain(tmp_path):
    store, state = _store(tmp_path)

    store.append_trace(state, {"event": "run_started"})

    assert store.read_trace(state.run_id)[0]["prev_hash"] == TRACE_CHAIN_GENESIS


def test_tail_scan_reads_only_the_last_line(tmp_path):
    """反向读末行：不该把整份 trace 解析一遍。"""
    store, state = _store(tmp_path)
    for index in range(50):
        store.append_trace(state, {"event": "step", "index": index})

    reopened = RunStore(tmp_path / "runs")
    calls = []
    original = reopened.read_trace
    reopened.read_trace = lambda run_id: calls.append(run_id) or original(run_id)

    reopened.append_trace(state, {"event": "step", "index": 50})

    assert calls == []


def test_partial_trailing_line_falls_back_to_full_scan(tmp_path):
    """上次崩在写一半：末行不是合法 JSON，退回全量读一次，序号仍然正确。"""
    store, state = _store(tmp_path)
    store.append_trace(state, {"event": "a"})
    store.append_trace(state, {"event": "b"})
    with store.trace_path(state.run_id).open("a", encoding="utf-8") as handle:
        handle.write('{"event": "half')

    reopened = RunStore(tmp_path / "runs")
    reopened.append_trace(state, {"event": "c"})

    events = reopened.read_trace(state.run_id)
    assert [event["event"] for event in events] == ["a", "b", "c"]
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_cache_is_per_run_id(tmp_path):
    store = RunStore(tmp_path / "runs")
    first = TaskState.create(run_id="run_a", task_id="t", user_request="x")
    second = TaskState.create(run_id="run_b", task_id="t", user_request="x")
    store.start_run(first)
    store.start_run(second)

    store.append_trace(first, {"event": "a"})
    store.append_trace(first, {"event": "a"})
    store.append_trace(second, {"event": "b"})

    assert [event["sequence"] for event in store.read_trace("run_a")] == [1, 2]
    assert [event["sequence"] for event in store.read_trace("run_b")] == [1]


def test_terminal_events_force_an_fsync(tmp_path, monkeypatch):
    """run 收尾/checkpoint 之后可以认为状态落地了，把摊派的 fsync 结清。"""
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
    import os

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    agent.ask("hello")

    trace = agent.run_store.read_trace(agent.current_task_state.run_id)
    assert any(event["event"] == "run_finished" for event in trace)
    assert synced


def test_trace_line_is_valid_json_per_line(tmp_path):
    store, state = _store(tmp_path)
    for index in range(5):
        store.append_trace(state, {"event": "step", "index": index})

    lines = store.trace_path(state.run_id).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert [json.loads(line)["index"] for line in lines] == [0, 1, 2, 3, 4]
