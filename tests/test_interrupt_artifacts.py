"""中断路径（spec-02 §4.7）：异常仍然向上抛，但工件必须齐全。"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.task_state import STATUS_FAILED, STOP_REASON_INTERRUPTED
from moss.tools import run_shell_command


class _ExplodingModelClient(FakeModelClient):
    """在第 k 次模型调用时抛出指定异常。"""

    def __init__(self, outputs, *, fail_at, exc):
        super().__init__(outputs)
        self.fail_at = fail_at
        self.exc = exc
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self.fail_at:
            raise self.exc
        return super().complete(*args, **kwargs)


def _build_agent(tmp_path, client):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )


def _artifacts(agent):
    task_state = agent.current_task_state
    run_dir = agent.run_store.run_dir(task_state)
    return (
        json.loads((run_dir / "task_state.json").read_text(encoding="utf-8")),
        [
            json.loads(line)
            for line in agent.run_store.trace_path(task_state).read_text(encoding="utf-8").splitlines()
        ],
        json.loads((run_dir / "report.json").read_text(encoding="utf-8")),
    )


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_interrupt_leaves_complete_artifacts_and_still_propagates(tmp_path, exc):
    agent = _build_agent(
        tmp_path,
        _ExplodingModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
                "<final>Done.</final>",
            ],
            fail_at=2,
            exc=exc,
        ),
    )

    with pytest.raises(type(exc)):
        agent.ask("Read the readme")

    task_state, trace, report = _artifacts(agent)
    assert task_state["status"] == STATUS_FAILED
    assert task_state["stop_reason"] == STOP_REASON_INTERRUPTED
    assert report["stop_reason"] == STOP_REASON_INTERRUPTED
    events = [event["event"] for event in trace]
    assert "run_interrupted" in events
    assert "run_finished" in events
    # 第一步的工具执行痕迹不能因为中断而丢失。
    assert "tool_executed" in events


def test_interrupt_sets_the_cancel_token(tmp_path):
    agent = _build_agent(
        tmp_path,
        _ExplodingModelClient(["<final>Done.</final>"], fail_at=1, exc=KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        agent.ask("anything")

    assert agent.cancel_token.is_set()


def test_a_new_run_clears_the_cancel_token(tmp_path):
    agent = _build_agent(tmp_path, FakeModelClient(["<final>Done.</final>"]))
    agent.cancel_token.set()

    assert agent.ask("anything") == "Done."
    assert not agent.cancel_token.is_set()


def test_model_backend_error_still_takes_the_model_error_path(tmp_path):
    """中断收尾不能把模型后端错误也吞成 interrupted。"""
    agent = _build_agent(
        tmp_path,
        _ExplodingModelClient(["<final>Done.</final>"], fail_at=1, exc=RuntimeError("boom")),
    )

    answer = agent.ask("anything")

    assert "Model backend error" in answer
    task_state, _, _ = _artifacts(agent)
    assert task_state["stop_reason"] == "model_error"


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups required")
def test_run_shell_timeout_kills_the_whole_process_group(tmp_path):
    """只杀 shell 会留下孤儿进程继续跑——那是最难排查的一类问题。"""
    marker = tmp_path / "orphan.txt"
    # 外层 shell 起一个后台子进程，自己立刻进入长睡眠：
    # 只 kill 外层的话，后台那个还会在 2 秒后写文件。
    command = f"(sleep 2; echo alive > {marker}) & sleep 30"

    with pytest.raises(subprocess.TimeoutExpired):
        run_shell_command(command, cwd=tmp_path, timeout=1, env=dict(os.environ))

    time.sleep(3)
    assert not marker.exists()


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups required")
def test_run_shell_stops_when_the_cancel_token_is_set(tmp_path):
    token = threading.Event()
    threading.Timer(0.3, token.set).start()

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled"):
        run_shell_command("sleep 30", cwd=tmp_path, timeout=30, env=dict(os.environ), cancel_token=token)

    assert time.monotonic() - started < 5


def test_run_shell_still_returns_output_and_exit_code(tmp_path):
    returncode, stdout, stderr = run_shell_command(
        f"{sys.executable} -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\"",
        cwd=tmp_path,
        timeout=30,
        env=dict(os.environ),
    )

    assert returncode == 3
    assert "out" in stdout
    assert "err" in stderr


def test_signal_module_is_available_for_group_termination():
    # 纯粹守住 import：漏了它，超时路径会在真正需要杀进程时才炸。
    assert hasattr(signal, "SIGKILL") or os.name == "nt"
