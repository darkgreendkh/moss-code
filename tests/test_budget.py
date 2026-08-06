"""多维预算（spec-02 §4.6）。"""

import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.agent.budget import RunBudget, usage_from_metadata
from moss.agent.state import STOP_REASON_BUDGET_EXCEEDED, TaskState


def test_hard_exceeded_reports_the_first_breached_dimension():
    budget = RunBudget(max_input_tokens=100, max_output_tokens=100)

    budget.consume(input_tokens=100, output_tokens=1)

    assert budget.hard_exceeded() == "input_tokens"


def test_nothing_is_exceeded_when_no_limits_are_set():
    budget = RunBudget()

    budget.consume(input_tokens=10**9, output_tokens=10**9, elapsed_s=10**6)

    assert budget.hard_exceeded() is None
    assert budget.soft_exceeded() is None


def test_soft_threshold_fires_at_eighty_percent_and_only_once():
    budget = RunBudget(max_input_tokens=100)

    budget.consume(input_tokens=80)
    first = budget.soft_exceeded()
    budget.consume(input_tokens=5)
    second = budget.soft_exceeded()

    assert first == "input_tokens"
    # 每轮都喊等于把剩下的预算花在喊话上。
    assert second is None


def test_unknown_price_stays_none_instead_of_zero():
    """把未知当成 0 会让金额上限永远不触发，正好在最该拦的时候不拦。"""
    budget = RunBudget(max_usd=1.0)

    budget.consume(input_tokens=10**6, usd=None)

    assert budget.usd is None
    assert budget.hard_exceeded() is None
    assert budget.snapshot()["usd"] is None


def test_known_prices_accumulate_and_can_breach():
    budget = RunBudget(max_usd=0.05)

    budget.consume(usd=0.03)
    budget.consume(usd=0.03)

    assert budget.hard_exceeded() == "usd"
    assert abs(budget.usd - 0.06) < 1e-9


def test_wall_clock_is_absolute_not_incremental():
    budget = RunBudget(max_wall_clock_s=10)

    budget.consume(elapsed_s=4)
    budget.consume(elapsed_s=6)

    assert budget.elapsed_s == 6


def test_usage_from_metadata_prefers_real_usage():
    tokens = usage_from_metadata({"input_tokens": 11, "output_tokens": 7})

    assert tokens == (11, 7, False)


def test_usage_from_metadata_falls_back_to_estimation_and_says_so():
    """report 里分不清真实 usage 和估算，成本口径就没法信。"""
    tokens = usage_from_metadata({}, prompt="abcd" * 10, completion="ef", measure=len)

    assert tokens == (40, 2, True)


def test_snapshot_carries_limits_and_the_estimated_flag():
    budget = RunBudget(max_steps=5, max_input_tokens=100)
    budget.consume(steps=1, input_tokens=10, estimated=True)

    snapshot = budget.snapshot()

    assert snapshot["steps"] == 1
    assert snapshot["max_input_tokens"] == 100
    assert snapshot["usage_estimated"] is True


def _build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        max_steps=10,
        **kwargs,
    )


class _CountingClient(FakeModelClient):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return super().complete(*args, **kwargs)


def test_hard_budget_stops_before_calling_the_model_again(tmp_path):
    """超限后再发一次请求，很可能正好把预算捅穿。"""
    agent = _build_agent(
        tmp_path,
        ['<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "<final>Done.</final>"],
        run_budget_limits={"max_output_tokens": 1},
    )
    client = _CountingClient(
        ['<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "<final>Done.</final>"]
    )
    agent.model_client = client

    answer = agent.ask("do work")

    assert client.calls == 1
    assert "budget" in answer
    assert agent.current_task_state.stop_reason == STOP_REASON_BUDGET_EXCEEDED


def test_budget_exceeded_still_writes_complete_artifacts(tmp_path):
    agent = _build_agent(
        tmp_path,
        ['<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "<final>Done.</final>"],
        run_budget_limits={"max_output_tokens": 1},
    )

    agent.ask("do work")

    run_dir = agent.run_store.run_dir(agent.current_task_state)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]

    assert report["stop_reason"] == STOP_REASON_BUDGET_EXCEEDED
    assert report["usage"]["output_tokens"] >= 1
    assert [event["event"] for event in events].count("budget_exceeded") == 1
    assert "run_finished" in [event["event"] for event in events]


def test_soft_budget_asks_the_model_to_converge(tmp_path):
    agent = _build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>Done.</final>",
        ],
        run_budget_limits={"max_output_tokens": 20},
    )

    agent.ask("do work")

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    soft = [event for event in events if event["event"] == "budget_soft_exceeded"]
    assert soft and soft[0]["dimension"] == "output_tokens"
    notices = [
        item
        for item in agent.session["history"]
        if item.get("role") == "system" and "budget" in item.get("content", "")
    ]
    assert len(notices) == 1


def test_no_limits_means_the_previous_behaviour(tmp_path):
    agent = _build_agent(tmp_path, ["<final>Done.</final>"])

    assert agent.ask("do work") == "Done."
    assert agent.current_task_state.stop_reason == "final_answer_returned"


def test_graceful_final_names_what_was_done(tmp_path):
    from moss.agent.budget import graceful_final

    state = TaskState.create(task_id="t", user_request="x")
    state.record_model_turn()
    state.record_tool("read_file")

    text = graceful_final(state, "wall_clock_s")

    assert "wall_clock_s" in text
    assert "read_file" in text
    assert "next step" in text.lower()
