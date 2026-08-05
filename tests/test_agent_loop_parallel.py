"""批内并发（spec-02 §4.1）。核心断言是顺序不变量，不是速度。"""

import json
import threading
import time

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext


def _build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    kwargs.setdefault("max_steps", 10)
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        **kwargs,
    )


_THREE_READS = (
    '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
    '<tool>{"name":"read_file","args":{"path":"b.py"}}</tool>\n'
    '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>'
)


def _tool_history(agent):
    return [
        (item["name"], item["args"].get("path"))
        for item in agent.session["history"]
        if item.get("role") == "tool"
    ]


def _trace(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]


def test_three_reads_are_one_model_turn(tmp_path):
    """一批三个读操作只该花一次模型调用，这正是批执行的意义。"""
    agent = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"], parallel_tools=True)

    assert agent.ask("read them all") == "Done."

    assert agent.current_task_state.model_turns == 2
    assert agent.current_task_state.tool_steps == 3
    assert _tool_history(agent) == [("read_file", "a.py"), ("read_file", "b.py"), ("read_file", "c.py")]


def test_history_order_follows_action_index_not_completion_order(tmp_path):
    """并发下记录顺序必须恒定，否则录制回放不成立。"""
    for _ in range(20):
        agent = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"], parallel_tools=True)
        agent.ask("read them all")

        assert _tool_history(agent) == [
            ("read_file", "a.py"),
            ("read_file", "b.py"),
            ("read_file", "c.py"),
        ]
        executed = [event["args"]["path"] for event in _trace(agent) if event["event"] == "tool_executed"]
        assert executed == ["a.py", "b.py", "c.py"]


def test_parallel_and_serial_produce_identical_history(tmp_path):
    parallel = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"], parallel_tools=True)
    parallel.ask("read them all")
    serial = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"], parallel_tools=False)
    serial.ask("read them all")

    assert _tool_history(parallel) == _tool_history(serial)


def test_read_only_batch_actually_runs_concurrently(tmp_path):
    """并发得是真并发——串行执行时这个断言会因为线程数只有 1 而失败。"""
    agent = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"], parallel_tools=True)
    threads = set()
    barrier = threading.Barrier(3, timeout=5)
    # 包在 execute_tool 上而不是工具注册表上：每轮 refresh_prefix 都会重建
    # agent.tools，改注册表会被下一轮覆盖掉。
    original = agent.execute_tool

    def instrumented(name, args, **kwargs):
        threads.add(threading.get_ident())
        barrier.wait()
        return original(name, args, **kwargs)

    agent.execute_tool = instrumented
    agent.ask("read them all")

    assert len(threads) == 3


def test_a_risky_tool_downgrades_the_whole_batch_to_serial(tmp_path):
    """risky 工具要走审批、要前后各拍一次快照，并发会让 diff 归属不清。"""
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
        '<tool>{"name":"write_file","args":{"path":"out.txt","content":"x"}}</tool>\n'
        '<tool>{"name":"read_file","args":{"path":"b.py"}}</tool>'
    )
    agent = _build_agent(tmp_path, [raw, "<final>Done.</final>"], parallel_tools=True)
    approvals = []
    agent.approve = lambda name, args: approvals.append(name) or True

    agent.ask("mixed batch")

    batch_started = [event for event in _trace(agent) if event["event"] == "tools_batch_started"]
    assert batch_started[0]["parallel"] is False
    assert approvals == ["write_file"]
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x"


def test_parallel_is_off_by_default(tmp_path):
    agent = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"])

    agent.ask("read them all")

    assert [event["parallel"] for event in _trace(agent) if event["event"] == "tools_batch_started"] == [False]


def test_batch_stops_at_the_step_budget(tmp_path):
    """一批动作不能把 25 步的预算变成 100 次工具调用。"""
    agent = _build_agent(
        tmp_path,
        [_THREE_READS, "<final>Done.</final>"],
        parallel_tools=True,
        max_steps=2,
    )

    agent.ask("read them all")

    assert agent.current_task_state.tool_steps == 2


def test_a_failing_action_does_not_stop_the_rest_of_the_batch(tmp_path):
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
        '<tool>{"name":"read_file","args":{"path":"missing.py"}}</tool>\n'
        '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>'
    )
    agent = _build_agent(tmp_path, [raw, "<final>Done.</final>"], parallel_tools=True)

    agent.ask("read them all")

    statuses = [event["tool_status"] for event in _trace(agent) if event["event"] == "tool_executed"]
    assert statuses[0] == "ok"
    # 不存在的路径在校验阶段就被挡下（rejected），关键是它不影响后面那个。
    assert statuses[1] != "ok"
    assert statuses[2] == "ok"


def test_batch_traces_report_count_and_duration(tmp_path):
    agent = _build_agent(tmp_path, [_THREE_READS, "<final>Done.</final>"], parallel_tools=True)

    started_at = time.monotonic()
    agent.ask("read them all")
    elapsed_ms = int((time.monotonic() - started_at) * 1000)

    finished = [event for event in _trace(agent) if event["event"] == "tools_batch_finished"]
    assert finished[0]["count"] == 3
    assert 0 <= finished[0]["duration_ms"] <= elapsed_ms
