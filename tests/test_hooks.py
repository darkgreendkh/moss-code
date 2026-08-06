"""用户钩子（spec-09 §9.5）。

守三条验收：钩子超时不阻断主流程、`pre_tool` 退出码 2 拒绝调用并记事件、
agent 无法写入 `.moss/hooks/`。
"""

import json
import os
import stat

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs.observability import events as trace_events
from moss.hooks import HOOK_POINTS, HookOutcome, find_hook, run_hook


def _write_hook(root, point, script, *, executable=True, suffix=""):
    hooks_dir = root / ".moss" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / f"{point}{suffix}"
    path.write_text(script, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _agent(tmp_path, outputs=(), **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


# --- 发现 ---------------------------------------------------------------


def test_finds_a_hook_by_point(tmp_path):
    path = _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nexit 0\n")

    assert find_hook(tmp_path, "pre_tool") == path


def test_finds_suffixed_hooks(tmp_path):
    _write_hook(tmp_path, "post_run", "#!/bin/sh\nexit 0\n", suffix=".sh")

    assert find_hook(tmp_path, "post_run") is not None


def test_non_executable_files_are_ignored(tmp_path):
    """没加 +x 的脚本更可能是半成品，拿 sh 去跑它是替用户做决定。"""
    _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nexit 2\n", executable=False)

    assert find_hook(tmp_path, "pre_tool") is None


def test_missing_hook_is_a_no_op(tmp_path):
    outcome = run_hook(tmp_path, "pre_tool", {"tool": "read_file"})

    assert outcome.ran is False and outcome.denied is False


# --- 执行纪律 -----------------------------------------------------------


def test_hook_receives_the_payload_on_stdin(tmp_path):
    _write_hook(tmp_path, "pre_tool", "#!/bin/sh\ncat > payload.json\nexit 0\n")

    run_hook(tmp_path, "pre_tool", {"tool": "read_file", "args": {"path": "x"}})

    assert json.loads((tmp_path / "payload.json").read_text())["tool"] == "read_file"


def test_timeout_does_not_block_the_main_flow(tmp_path):
    _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nsleep 5\n")

    outcome = run_hook(tmp_path, "pre_tool", {}, timeout=1)

    assert outcome.ran is True
    assert outcome.denied is False
    assert "timed out" in outcome.error


def test_a_crashing_hook_does_not_deny(tmp_path):
    """非 2 的退出码只是"钩子自己失败了"，不该改变控制流。"""
    _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nexit 1\n")

    outcome = run_hook(tmp_path, "pre_tool", {})

    assert outcome.exit_code == 1 and outcome.denied is False


def test_only_pre_tool_can_deny(tmp_path):
    for point in HOOK_POINTS:
        _write_hook(tmp_path, point, "#!/bin/sh\nexit 2\n")
        assert run_hook(tmp_path, point, {}).denied is (point == "pre_tool")


# --- 与主循环的接线 -----------------------------------------------------


def test_pre_tool_exit_two_denies_the_call_and_records_the_event(tmp_path):
    _write_hook(tmp_path, "pre_tool", '#!/bin/sh\necho "no reading today" >&2\nexit 2\n')
    agent = _agent(tmp_path, ['<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "<final>ok</final>"])

    agent.ask("read it")

    trace = agent.run_store.read_trace(agent.current_task_state.run_id)
    denied = [event for event in trace if event["event"] == trace_events.HOOK_DENIED]
    assert denied and denied[0]["point"] == "pre_tool"
    tool_event = next(event for event in trace if event["event"] == trace_events.TOOL_EXECUTED)
    assert tool_event["tool_error_code"] == "hook_denied"
    assert "no reading today" in tool_event["result"]


def test_post_tool_cannot_deny_anything(tmp_path):
    _write_hook(tmp_path, "post_tool", "#!/bin/sh\nexit 2\n")
    agent = _agent(tmp_path, ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>ok</final>"])

    assert agent.ask("look") == "ok"


def test_hooks_that_run_before_the_report_land_in_it(tmp_path):
    _write_hook(tmp_path, "pre_final", "#!/bin/sh\nexit 0\n")
    agent = _agent(tmp_path, ["<final>done</final>"])

    agent.ask("hello")

    points = [entry["point"] for entry in agent.run_store.load_report(agent.current_task_state.run_id)["hooks"]]
    assert "pre_final" in points


def test_post_run_fires_after_the_report_and_lands_in_the_trace(tmp_path):
    """post_run 挂在 finally 上，晚于 report 落盘——所以它的证据在 trace 里。"""
    _write_hook(tmp_path, "post_run", "#!/bin/sh\nexit 0\n")
    agent = _agent(tmp_path, ["<final>done</final>"])

    agent.ask("hello")

    trace = agent.run_store.read_trace(agent.current_task_state.run_id)
    ran = [event for event in trace if event["event"] == trace_events.HOOK_RAN]
    assert [event["point"] for event in ran] == ["post_run"]
    # 它确实排在 run_finished 之后。
    assert trace.index(ran[0]) > max(
        index for index, event in enumerate(trace) if event["event"] == trace_events.RUN_FINISHED
    )


def test_post_run_fires_even_when_the_run_is_interrupted(tmp_path):
    class _Cancelling(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            raise KeyboardInterrupt

    _write_hook(tmp_path, "post_run", "#!/bin/sh\ntouch post-run-ran\nexit 0\n")
    agent = _agent(tmp_path)
    agent.model_client = _Cancelling([])

    try:
        agent.ask("hello")
    except KeyboardInterrupt:
        pass

    assert (tmp_path / "post-run-ran").exists()


def test_pre_final_runs_before_the_answer_is_recorded(tmp_path):
    _write_hook(tmp_path, "pre_final", "#!/bin/sh\ntouch pre-final-ran\nexit 0\n")
    agent = _agent(tmp_path, ["<final>done</final>"])

    agent.ask("hello")

    assert (tmp_path / "pre-final-ran").exists()


def test_hook_payload_is_redacted(tmp_path, monkeypatch):
    """钩子是用户的脚本，不是可信执行环境。"""
    monkeypatch.setenv("MOSS_OPENAI_API_KEY", "super-secret-value-42")
    _write_hook(tmp_path, "pre_tool", "#!/bin/sh\ncat > payload.json\nexit 0\n")
    agent = _agent(tmp_path, secret_env_names=("MOSS_OPENAI_API_KEY",))

    agent.fire_hook("pre_tool", {"tool": "run_shell", "args": {"command": "echo super-secret-value-42"}})

    text = (tmp_path / "payload.json").read_text()
    assert "super-secret-value-42" not in text
    assert "<redacted>" in text


def test_a_slow_hook_does_not_break_the_run(tmp_path):
    _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nsleep 30\n")
    agent = _agent(tmp_path, ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>ok</final>"])

    # 3 秒超时之后照常执行；这一条要是挂住，整个测试会跑 30 秒。
    assert agent.ask("look") == "ok"


# --- agent 不能给自己装后门 ----------------------------------------------


def test_agent_cannot_write_into_the_hooks_directory(tmp_path):
    """否则 agent 能往 .moss/hooks/ 塞一个自己的 pre_tool。"""
    agent = _agent(tmp_path)

    result = agent.run_tool(
        "write_file", {"path": ".moss/hooks/pre_tool", "content": "#!/bin/sh\nexit 0\n"}
    )

    assert "denied by policy" in result
    assert not (tmp_path / ".moss" / "hooks" / "pre_tool").exists()


def test_agent_cannot_edit_an_existing_hook(tmp_path):
    path = _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nexit 0\n")
    agent = _agent(tmp_path)

    result = agent.run_tool(
        "edit_file", {"path": ".moss/hooks/pre_tool", "old_text": "exit 0", "new_text": "exit 2"}
    )

    assert "denied by policy" in result
    assert "exit 0" in path.read_text(encoding="utf-8")


def test_outcome_reason_is_bounded():
    """钩子往 stderr 打了一兆日志，不该整份进 trace。"""
    outcome = HookOutcome("pre_tool", ran=True, denied=True, exit_code=2, stderr="x" * 10000)

    assert len(outcome.to_dict()["reason"]) <= 400


def test_hook_env_is_filtered(tmp_path):
    """钩子继承的是 run_shell 那份 allowlist，不是整个进程环境。"""
    os.environ["MOSS_HOOK_LEAK_CANARY"] = "leaked"
    try:
        _write_hook(tmp_path, "pre_tool", "#!/bin/sh\nenv > env.txt\nexit 0\n")
        _agent(tmp_path).fire_hook("pre_tool", {})
    finally:
        del os.environ["MOSS_HOOK_LEAK_CANARY"]

    assert "MOSS_HOOK_LEAK_CANARY" not in (tmp_path / "env.txt").read_text()
