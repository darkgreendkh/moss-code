"""trace 事件名的契约（spec-07 §4.5）。

事件名是 trace/report 的对外契约。用字面量匹配时，改一个名字不会报错——
只会让某个指标悄悄变成 0，而且没人会注意到。这里让它在**测试期**就炸掉。
"""

import ast
import pathlib

from moss.runs.observability import events as trace_events
from moss.runs.store import RunStore
from moss.task_state import TaskState

EVALUATION_DIR = pathlib.Path(trace_events.__file__).parent / "evaluation"


def _string_literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_evaluation_never_hardcodes_a_known_event_name():
    offenders = []
    for path in sorted(EVALUATION_DIR.rglob("*.py")):
        for lineno, value in _string_literals(path):
            if value in trace_events.ALL_EVENTS:
                offenders.append(f"{path.name}:{lineno}: {value!r}")

    assert not offenders, (
        "评测侧必须 from moss.runs.observability.events import ...，不许写字面量：\n"
        + "\n".join(offenders)
    )


def test_runtime_never_hardcodes_an_event_name_in_emit_trace():
    """主循环里的 emit_trace 也一样：字面量会让改名变成静默失真。"""
    offenders = []
    root = pathlib.Path(trace_events.__file__).parent
    for path in sorted(root.rglob("*.py")):
        if path.name == "trace_events.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", "")
            if name not in ("emit_trace", "append_trace", "record_memory_event"):
                continue
            index = 0 if name == "record_memory_event" else 1
            if len(node.args) <= index:
                continue
            argument = node.args[index]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                offenders.append(f"{path.name}:{argument.lineno}: {argument.value!r}")

    assert not offenders, "\n".join(offenders)


def test_all_events_covers_every_declared_constant():
    declared = {
        value
        for name, value in vars(trace_events).items()
        if name.isupper() and isinstance(value, str)
    }

    # 加了常量却忘了登记，或登记了不存在的名字，都会在这里炸。
    assert declared == set(trace_events.ALL_EVENTS)


def test_every_name_in_all_is_exported():
    for name in trace_events.__all__:
        assert hasattr(trace_events, name), name


def test_emitted_events_carry_schema_version_and_a_known_name(tmp_path):
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient(
            ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>done</final>"]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )

    agent.ask("look around")

    trace = agent.run_store.read_trace(agent.current_task_state.run_id)
    assert trace
    unknown = sorted({event["event"] for event in trace} - trace_events.ALL_EVENTS)
    assert not unknown, f"trace 里出现了未登记的事件名: {unknown}"
    assert all(
        event["schema_version"] == trace_events.TRACE_SCHEMA_VERSION for event in trace
    )


def test_schema_version_is_stamped_on_raw_appends(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = TaskState.create(run_id="run_v", task_id="t", user_request="x")
    store.start_run(state)

    store.append_trace(state, {"event": trace_events.RUN_STARTED})

    event = store.read_trace(state.run_id)[0]
    assert event["schema_version"] == trace_events.TRACE_SCHEMA_VERSION


def test_explicit_schema_version_is_not_overwritten(tmp_path):
    """兼容读取旧工件时可以按原版本重放，落盘的版本号不该被改写。"""
    store = RunStore(tmp_path / "runs")
    state = TaskState.create(run_id="run_v1", task_id="t", user_request="x")
    store.start_run(state)

    store.append_trace(state, {"event": trace_events.RUN_STARTED, "schema_version": 1})

    assert store.read_trace(state.run_id)[0]["schema_version"] == 1
