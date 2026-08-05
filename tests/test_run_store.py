import json

from moss.run_store import TRACE_CHAIN_GENESIS, RunStore, event_digest
from moss.task_state import (
    STATUS_FAILED,
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_INTERRUPTED,
    TaskState,
)


def test_run_store_creates_run_directory_and_state_file(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    run_dir = store.start_run(state)

    assert run_dir == store.run_dir(state.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(state)

    store.append_trace(state, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"})
    store.append_trace(
        state.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "prompt_metadata": {"prompt_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(state.run_id, {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"})

    lines = (store.trace_path(state.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


def test_run_store_adds_trace_identity_fields(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_identity", task_id="task_identity", user_request="Trace identity.")
    state.record_attempt()
    state.record_tool("read_file")
    store.start_run(state)

    store.append_trace(state, {"event": "tool_executed"})
    state.record_attempt()
    store.append_trace(state, {"event": "checkpoint_created"})

    events = store.read_trace(state.run_id)
    assert [event["sequence"] for event in events] == [1, 2]
    assert events[0]["event_id"] == "run_identity:000001"
    assert events[0]["run_id"] == "run_identity"
    assert events[0]["task_id"] == "task_identity"
    assert events[0]["attempt"] == 1
    assert events[0]["tool_steps"] == 1
    assert events[1]["attempt"] == 2


def test_run_store_read_trace_ignores_trailing_partial_json_line(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_partial_trace", task_id="task_partial_trace", user_request="Read trace.")
    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})
    store.append_trace(state, {"event": "prompt_built"})
    with store.trace_path(state.run_id).open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write('{"event": "tool_executed"')

    events = store.read_trace(state.run_id)

    assert [event["event"] for event in events] == ["run_started", "prompt_built"]


def test_run_store_marks_running_runs_interrupted_with_audit_report(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    running = TaskState.create(run_id="run_interrupted", task_id="task_interrupted", user_request="Crash.")
    completed = TaskState.create(run_id="run_completed", task_id="task_completed", user_request="Done.")
    completed.finish_success("ok")
    store.start_run(running)
    store.start_run(completed)
    store.append_trace(running, {"event": "tool_executed"})

    interrupted = store.mark_interrupted_runs()

    assert [item["run_id"] for item in interrupted] == ["run_interrupted"]
    persisted = store.load_task_state(running.run_id)
    assert persisted["status"] == STATUS_FAILED
    assert persisted["stop_reason"] == STOP_REASON_INTERRUPTED
    report = store.load_report(running.run_id)
    assert report["stop_reason"] == STOP_REASON_INTERRUPTED
    assert report["last_complete_event"]["event"] == "tool_executed"


def test_run_store_writes_report_json(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Report the run.")
    store.start_run(state)
    state.finish_success("Done.")

    store.write_task_state(state)
    store.write_report(state, {"task_state": state.to_dict(), "stop_reason": state.stop_reason})

    report = json.loads(store.report_path(state.run_id).read_text(encoding="utf-8"))
    assert report["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert report["task_state"]["final_answer"] == "Done."


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})

    assert store.trace_path(state.run_id).exists()
    assert not store.report_path(state.run_id).exists()


def test_trace_events_form_a_hash_chain(tmp_path):
    """trace 是审计工件：谁能悄悄改一条，整份工件就没有证据价值了。"""
    store = RunStore(tmp_path / "runs")
    task_state = TaskState.create(task_id="t", user_request="x")
    store.start_run(task_state)
    for index in range(3):
        store.append_trace(task_state, {"event": "step", "index": index})

    events = store.read_trace(task_state.run_id)
    ok, problems = store.verify_trace(task_state.run_id)

    assert ok, problems
    assert events[0]["prev_hash"] == TRACE_CHAIN_GENESIS
    assert events[1]["prev_hash"] == event_digest(events[0])


def test_tampering_with_a_middle_event_breaks_the_chain(tmp_path):
    store = RunStore(tmp_path / "runs")
    task_state = TaskState.create(task_id="t", user_request="x")
    store.start_run(task_state)
    for index in range(3):
        store.append_trace(task_state, {"event": "step", "index": index})

    path = store.trace_path(task_state.run_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["index"] = 999
    lines[1] = json.dumps(tampered, sort_keys=True, ensure_ascii=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problems = store.verify_trace(task_state.run_id)

    assert ok is False
    assert problems


def test_deleting_an_event_is_detected(tmp_path):
    store = RunStore(tmp_path / "runs")
    task_state = TaskState.create(task_id="t", user_request="x")
    store.start_run(task_state)
    for index in range(3):
        store.append_trace(task_state, {"event": "step", "index": index})

    path = store.trace_path(task_state.run_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    ok, problems = store.verify_trace(task_state.run_id)

    assert ok is False
    assert any("sequence gap" in problem or "broken chain" in problem for problem in problems)


def test_digest_ignores_prev_hash_itself():
    event = {"event": "a", "sequence": 1}

    assert event_digest(event) == event_digest({**event, "prev_hash": "whatever"})
