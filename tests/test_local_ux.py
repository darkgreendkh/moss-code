"""交互式本地 agent 的使用体验优化的回归测试。

覆盖这一批改动里没有其它测试守着、又容易悄悄回退的行为：跨会话审批持久化、
REPL 内 /resume、/config 的安全姿态行、多行输入、进度 spinner 的耗时。
"""

import builtins
import io
import time
import types

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.cli.repl import (
    apply_resume,
    make_progress_printer,
    read_user_input,
    render_config,
    render_sessions,
)
from moss.execution import service as svc


def _build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


# ---- 跨会话审批持久化 ----


def test_persisted_approvals_keep_low_risk_allows_and_all_denies(tmp_path):
    memory = {
        ("run_shell", "read_only", "git"): True,   # 低风险 allow：留
        ("run_shell", "high", "rm"): True,          # 高风险 allow：丢
        ("run_shell", "write", "touch"): False,     # deny：一律留（收紧总是安全）
        ("write_file", "high", "src"): True,        # 高风险 allow：丢
    }
    svc.save_persisted_approvals(tmp_path, memory)
    back = svc.load_persisted_approvals(tmp_path)
    assert back == {
        ("run_shell", "read_only", "git"): True,
        ("run_shell", "write", "touch"): False,
    }


def test_persisted_approvals_clear_removes_file(tmp_path):
    svc.save_persisted_approvals(tmp_path, {("run_shell", "test", "pytest"): True})
    assert svc.load_persisted_approvals(tmp_path)
    svc.clear_persisted_approvals(tmp_path)
    assert svc.load_persisted_approvals(tmp_path) == {}


def test_persisted_approvals_reject_tampered_high_risk_allow(tmp_path):
    # 手改文件塞一条高危 allow：加载时必须按风险重新校验并丢掉，绝不静默放行。
    path = tmp_path / ".moss" / svc.APPROVALS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"decisions":[{"name":"run_shell","risk":"high","bucket":"rm","allowed":true}]}',
        encoding="utf-8",
    )
    assert svc.load_persisted_approvals(tmp_path) == {}


def test_agent_loads_persisted_approvals_on_start(tmp_path):
    svc.save_persisted_approvals(tmp_path, {("run_shell", "read_only", "git"): True})
    agent = _build_agent(tmp_path)
    assert agent.remembered_approvals() == {("run_shell", "read_only", "git"): True}


# ---- REPL 内 /resume ----


def test_resume_restores_history_and_memory_in_place(tmp_path):
    first = _build_agent(tmp_path)
    first.session["history"].append({"role": "user", "content": "first task"})
    first.session_store.save(first.session)
    first_id = first.session["id"]

    # 复用同一个 store 模拟"重开 moss"：新会话，空历史。
    second = Moss(
        model_client=FakeModelClient([]),
        workspace=first.workspace,
        session_store=first.session_store,
        approval_policy="auto",
    )
    assert second.session["id"] != first_id
    assert second.session["history"] == []

    resumed = second.resume(first_id)
    assert resumed == first_id
    assert second.session["id"] == first_id
    assert [h["content"] for h in second.session["history"]] == ["first task"]
    assert second.memory.session_id == first_id


def test_apply_resume_reports_unknown_id(tmp_path):
    agent = _build_agent(tmp_path)
    assert "no such session" in apply_resume(agent, "nope")


def test_apply_resume_latest(tmp_path):
    agent = _build_agent(tmp_path)
    agent.session_store.save(agent.session)
    assert "resumed session" in apply_resume(agent, "latest")


def test_apply_resume_requires_argument(tmp_path):
    agent = _build_agent(tmp_path)
    assert apply_resume(agent, "") == "usage: /resume <id>|latest"


def test_render_sessions_marks_current(tmp_path):
    agent = _build_agent(tmp_path)
    agent.session_store.save(agent.session)
    rendered = render_sessions(agent)
    assert agent.session["id"] in rendered
    assert "* " + agent.session["id"] in rendered.replace("  ", " ")


# ---- /config 安全姿态 ----


def test_render_config_shows_security_rows():
    agent = types.SimpleNamespace(
        model_client=types.SimpleNamespace(model="m", provider="deepseek"),
        approval_policy="ask",
        verify_before_final=True,
        max_steps=25,
        sandbox_plan=types.SimpleNamespace(mode="sandbox-exec", degraded=False),
        allowed_network_hosts=(),
        injection_scan=True,
        run_budget_limits={"max_usd": 0.5},
        workspace=types.SimpleNamespace(cwd="/repo", branch="main"),
        session={"id": "abc"},
    )
    rendered = render_config(agent)
    assert "sandbox" in rendered and "sandbox-exec" in rendered
    assert "network" in rendered and "unrestricted" in rendered
    assert "injection" in rendered
    assert "budget" in rendered and "$0.5" in rendered


def test_render_config_flags_degraded_sandbox_and_network_allowlist():
    agent = types.SimpleNamespace(
        model_client=types.SimpleNamespace(model="m", provider="p"),
        approval_policy="auto",
        verify_before_final=False,
        max_steps=10,
        sandbox_plan=types.SimpleNamespace(mode="none", degraded=True),
        allowed_network_hosts=("example.com", "pypi.org"),
        injection_scan=False,
        run_budget_limits={},
        workspace=types.SimpleNamespace(cwd="/w", branch="dev"),
        session={"id": "x"},
    )
    rendered = render_config(agent)
    assert "none (degraded)" in rendered
    assert "example.com, pypi.org" in rendered
    assert "budget" in rendered and "none" in rendered


# ---- 多行输入 ----


def _feed_input(monkeypatch, lines):
    it = iter(lines)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))


def test_read_user_input_backslash_continuation(monkeypatch):
    _feed_input(monkeypatch, ["fix bug \\", "in foo.py"])
    assert read_user_input("moss> ", lambda s: s) == "fix bug \nin foo.py"


def test_read_user_input_triple_quote_block(monkeypatch):
    _feed_input(monkeypatch, ['"""', "line one", "line two", '"""'])
    assert read_user_input("moss> ", lambda s: s) == "line one\nline two"


def test_read_user_input_plain_single_line(monkeypatch):
    _feed_input(monkeypatch, ["just a task"])
    assert read_user_input("moss> ", lambda s: s) == "just a task"


# ---- 进度 spinner 的耗时 ----


def test_progress_thinking_reports_elapsed_seconds():
    buf = io.StringIO()
    printer = make_progress_printer(buf)
    printer("thinking", {"step": 1, "max_steps": 25})
    time.sleep(1.2)  # 让后台 ticker 至少跳一次
    printer("tool", {"name": "run_shell", "args": {"command": "pytest"}})
    printer.clear()
    out = buf.getvalue()
    assert "thinking (1/25" in out
    assert "s)" in out  # 带耗时后缀
    assert "run_shell" in out


def test_progress_ticker_thread_swallows_stream_errors():
    # 新增的后台耗时 ticker 会自己往 stream 写，它抛出的异常必须被吞掉
    # （线程里的异常没人接，泄漏出去会打一坨 traceback 到 stderr）。
    class BrokenStream:
        def write(self, *_):
            raise OSError("boom")

        def flush(self):
            raise OSError("boom")

    printer = make_progress_printer(BrokenStream())
    printer("thinking", {"step": 1, "max_steps": 3})
    time.sleep(1.2)  # 让 ticker 至少往坏 stream 写一次
    printer.clear()  # 停表也不该抛


def test_emit_progress_swallows_observer_exceptions(tmp_path):
    # 不变量 #2：observer 异常绝不影响控制流。守卫在 emit_progress。
    agent = _build_agent(tmp_path)

    def boom(event, payload):
        raise RuntimeError("observer blew up")

    agent.progress_observer = boom
    agent.emit_progress("thinking", {"step": 1, "max_steps": 3})  # 不抛即通过


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
