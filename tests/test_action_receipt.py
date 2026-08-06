"""动作意图/回执与崩溃后对账（spec-07 §4.6）。

覆盖崩溃窗口的四个边界：副作用前 / 副作用后回执前 / 回执后 / 完全没开始。
验收门槛（spec-07 §7）：崩溃后副作用重复次数 = 0。
"""

import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss import action_ledger
from moss import trace_events
from moss.run_store import RunStore
from moss.task_state import TaskState


def _agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )


def _actions(agent, run_id=None):
    run_id = run_id or agent.current_task_state.run_id
    events = agent.run_store.read_trace(run_id)
    return (
        [event for event in events if event["event"] == trace_events.ACTION_INTENT],
        [event for event in events if event["event"] == trace_events.ACTION_RECEIPT],
    )


def test_write_file_emits_a_paired_intent_and_receipt(tmp_path):
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"write_file","args":{"path":"a.txt","content":"hello"}}</tool>', "<final>done</final>"],
    )

    agent.ask("write it")

    intents, receipts = _actions(agent)
    assert len(intents) == 1 and len(receipts) == 1
    assert intents[0]["tool"] == "write_file"
    assert intents[0]["capabilities"] == ["fs_write"]
    assert intents[0]["idempotent"] is True
    # intent 里就要有"做完应该是什么样"，否则崩溃后只能一律转人工。
    assert intents[0]["intended_sha"] == action_ledger._sha256_text("hello")
    assert receipts[0]["action_id"] == intents[0]["action_id"]
    assert receipts[0]["status"] == "ok"
    assert receipts[0]["affected_paths"] == ["a.txt"]
    assert receipts[0]["after_sha"]["a.txt"] == intents[0]["intended_sha"]


def test_intent_precedes_the_side_effect_in_the_trace(tmp_path):
    """顺序不能反：意图必须先于副作用落盘，否则崩溃窗口里没有任何证据。"""
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"write_file","args":{"path":"a.txt","content":"hi"}}</tool>', "<final>done</final>"],
    )

    agent.ask("write it")

    names = [event["event"] for event in agent.run_store.read_trace(agent.current_task_state.run_id)]
    assert names.index(trace_events.ACTION_INTENT) < names.index(trace_events.ACTION_RECEIPT)
    assert names.index(trace_events.ACTION_RECEIPT) < names.index(trace_events.TOOL_EXECUTED)


def test_read_only_tools_are_not_ledgered(tmp_path):
    """只读工具没有副作用，为它们记账只是让 trace 体积翻倍。"""
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>done</final>"],
    )

    agent.ask("look")

    intents, receipts = _actions(agent)
    assert intents == [] and receipts == []


def test_failed_tool_still_gets_a_receipt(tmp_path):
    """没有回执的话，"跑了但报错"和"根本没跑"在恢复时长得一样。"""
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"exit 3"}}</tool>', "<final>done</final>"],
    )

    agent.ask("run it")

    intents, receipts = _actions(agent)
    assert len(intents) == len(receipts) == 1
    assert receipts[0]["status"] == "error"


def test_shell_receipt_carries_the_exit_code(tmp_path):
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"echo hi"}}</tool>', "<final>done</final>"],
    )

    agent.ask("run it")

    intents, receipts = _actions(agent)
    assert intents[0]["idempotent"] is False
    assert receipts[0]["exit_code"] == 0


# ---- 对账 ----


def _intent_event(tool, path, before, intended, idempotent=True):
    return {
        "event": trace_events.ACTION_INTENT,
        "action_id": "act_1",
        "tool": tool,
        "idempotent": idempotent,
        "expected_sha": {path: before},
        "intended_sha": intended,
        "idempotency_key": "sha256:key",
    }


def test_reconcile_treats_a_finished_action_as_done(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("after", encoding="utf-8")
    events = [
        _intent_event("write_file", "a.txt", action_ledger._sha256_text("before"), action_ledger._sha256_text("after")),
        {"event": trace_events.ACTION_RECEIPT, "action_id": "act_1"},
    ]

    assert action_ledger.reconcile(events, lambda rel: tmp_path / rel) == []


def test_reconcile_detects_a_side_effect_that_landed_without_a_receipt(tmp_path):
    """崩在"文件写完但回执没写"的窗口里：不能重放，否则副作用来两次。"""
    target = tmp_path / "a.txt"
    target.write_text("after", encoding="utf-8")
    events = [
        _intent_event("write_file", "a.txt", action_ledger._sha256_text("before"), action_ledger._sha256_text("after"))
    ]

    outcomes = action_ledger.reconcile(events, lambda rel: tmp_path / rel)

    assert [outcome["status"] for outcome in outcomes] == [action_ledger.STATUS_ALREADY_APPLIED]


def test_reconcile_marks_an_untouched_target_as_replayable(tmp_path):
    """崩在副作用之前：文件还是原样，可以安全重放。"""
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    events = [
        _intent_event("write_file", "a.txt", action_ledger._sha256_text("before"), action_ledger._sha256_text("after"))
    ]

    outcomes = action_ledger.reconcile(events, lambda rel: tmp_path / rel)

    assert [outcome["status"] for outcome in outcomes] == [action_ledger.STATUS_REPLAYABLE]


def test_reconcile_refuses_to_guess_when_a_third_party_changed_the_file(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("someone else wrote this", encoding="utf-8")
    events = [
        _intent_event("write_file", "a.txt", action_ledger._sha256_text("before"), action_ledger._sha256_text("after"))
    ]

    outcomes = action_ledger.reconcile(events, lambda rel: tmp_path / rel)

    assert outcomes[0]["status"] == action_ledger.STATUS_PENDING_UNKNOWN
    assert outcomes[0]["reason"] == "third_party_change"


def test_reconcile_never_auto_replays_a_shell_command(tmp_path):
    """重放 run_shell 可能是再删一次库。宁可多问一次。"""
    events = [_intent_event("run_shell", "", "", "", idempotent=False)]

    outcomes = action_ledger.reconcile(events, lambda rel: tmp_path / rel)

    assert outcomes[0]["status"] == action_ledger.STATUS_PENDING_UNKNOWN
    assert outcomes[0]["reason"] == "non_idempotent_tool"


def test_idempotency_key_is_stable_for_the_same_action():
    first = action_ledger.build_intent(name="write_file", args={"path": "a", "content": "x"}, previous_receipt_id="r1")
    second = action_ledger.build_intent(name="write_file", args={"path": "a", "content": "x"}, previous_receipt_id="r1")
    other = action_ledger.build_intent(name="write_file", args={"path": "a", "content": "y"}, previous_receipt_id="r1")

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["idempotency_key"] != other["idempotency_key"]
    # action_id 每次都新，才能在 trace 里区分两次调用。
    assert first["action_id"] != second["action_id"]


def test_startup_reconcile_surfaces_pending_unknown_actions(tmp_path):
    """整条链路：崩掉的 run 被接管时，未确认的动作要出现在报告和 checkpoint 里。"""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    runs_root = tmp_path / ".moss" / "runs"
    store = RunStore(runs_root, workspace_path=lambda rel: tmp_path / rel)
    state = TaskState.create(run_id="run_crashed", task_id="t", user_request="do things")
    store.start_run(state)
    store.append_trace(state, {"event": trace_events.ACTION_INTENT, "action_id": "act_1", "tool": "run_shell", "idempotent": False})
    store.lease.release(state.run_id)

    reopened = RunStore(runs_root, workspace_path=lambda rel: tmp_path / rel)
    taken = reopened.mark_interrupted_runs()

    assert len(taken) == 1
    pending = taken[0]["pending_actions"]
    assert [outcome["status"] for outcome in pending] == [action_ledger.STATUS_PENDING_UNKNOWN]
    # 对账结论本身也是审计事实。
    reconciled = [
        event
        for event in reopened.read_trace("run_crashed")
        if event["event"] == trace_events.ACTION_RECONCILED
    ]
    assert len(reconciled) == 1
    report = json.loads(reopened.report_path("run_crashed").read_text(encoding="utf-8"))
    assert report["pending_actions"] == pending


def test_checkpoint_text_lists_unverified_actions(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = RunStore(tmp_path / ".moss" / "runs", workspace_path=lambda rel: tmp_path / rel)
    state = TaskState.create(run_id="run_crashed", task_id="t", user_request="do things")
    store.start_run(state)
    store.append_trace(state, {"event": trace_events.ACTION_INTENT, "action_id": "act_1", "tool": "run_shell", "idempotent": False})
    store.lease.release(state.run_id)

    agent = _agent(tmp_path, [])

    text = agent.render_checkpoint_text()
    assert "unverified actions" in text
    assert "run_shell(non_idempotent_tool)" in text
