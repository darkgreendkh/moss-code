"""审批体验（spec-03 §4.5）。"""

import io
import json
from unittest.mock import patch

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext


def _build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "ask")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        **kwargs,
    )


class _FakeTty(io.StringIO):
    """假装是 /dev/tty：记录被问了什么，按预设脚本回答。"""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)
        self.questions = []

    def write(self, text):
        self.questions.append(text)
        return len(text)

    def readline(self, *args):
        return (self.answers.pop(0) + "\n") if self.answers else "\n"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_tty(answers):
    tty = _FakeTty(answers)
    return patch("builtins.open", lambda path, *a, **k: tty if str(path) == "/dev/tty" else io.StringIO()), tty


def test_yes_approves_once(tmp_path):
    agent = _build_agent(tmp_path)
    patcher, tty = _with_tty(["y", "n"])

    with patcher:
        first = agent.run_tool("write_file", {"path": "a.txt", "content": "x"})
        second = agent.run_tool("write_file", {"path": "b.txt", "content": "x"})

    assert first.startswith("wrote")
    assert "approval denied" in second
    assert len(tty.questions) == 2


def test_always_stops_asking_for_the_same_class(tmp_path):
    agent = _build_agent(tmp_path)
    patcher, tty = _with_tty(["a"])

    with patcher:
        first = agent.run_tool("write_file", {"path": "src/a.txt", "content": "x"})
        second = agent.run_tool("write_file", {"path": "src/b.txt", "content": "x"})

    assert first.startswith("wrote")
    assert second.startswith("wrote")
    assert len(tty.questions) == 1


def test_never_stops_asking_and_keeps_refusing(tmp_path):
    agent = _build_agent(tmp_path)
    patcher, tty = _with_tty(["d"])

    with patcher:
        first = agent.run_tool("write_file", {"path": "src/a.txt", "content": "x"})
        second = agent.run_tool("write_file", {"path": "src/b.txt", "content": "x"})

    assert "approval denied" in first
    assert "approval denied" in second
    assert len(tty.questions) == 1


def test_a_different_class_is_asked_again(tmp_path):
    """粒度太粗会让"允许一次"变成"允许一切"。"""
    agent = _build_agent(tmp_path)
    patcher, tty = _with_tty(["a", "n"])

    with patcher:
        agent.run_tool("write_file", {"path": "src/a.txt", "content": "x"})
        other = agent.run_tool("write_file", {"path": "docs/a.txt", "content": "x"})

    assert "approval denied" in other
    assert len(tty.questions) == 2


def test_shell_classes_are_keyed_by_the_executable(tmp_path):
    agent = _build_agent(tmp_path)

    assert agent.approval_class("run_shell", {"command": "git status"})[2] == "git"
    assert agent.approval_class("run_shell", {"command": "ls -la"})[2] == "ls"
    assert agent.approval_class("run_shell", {"command": "git status"}) != agent.approval_class(
        "run_shell", {"command": "rm x"}
    )


def test_no_tty_means_refusal(tmp_path):
    """读不清的回答绝不能默认放行。"""
    agent = _build_agent(tmp_path)

    def no_tty(path, *args, **kwargs):
        raise OSError("no tty here")

    with patch("builtins.open", no_tty), patch("sys.stdin", None):
        result = agent.run_tool("write_file", {"path": "a.txt", "content": "x"})

    assert "approval denied" in result


def test_piped_stdin_does_not_swallow_the_task_text(tmp_path):
    """`echo task | moss`：stdin 是管道时，input() 会把任务文本当成审批回答。"""
    agent = _build_agent(tmp_path)
    piped = io.StringIO("please refactor everything\n")
    piped.isatty = lambda: False

    def no_tty(path, *args, **kwargs):
        raise OSError("no tty here")

    with patch("builtins.open", no_tty), patch("sys.stdin", piped):
        result = agent.run_tool("write_file", {"path": "a.txt", "content": "x"})

    assert "approval denied" in result
    # 任务文本必须原封不动留在 stdin 里。
    assert piped.read() == "please refactor everything\n"


def test_approval_memory_is_never_persisted(tmp_path):
    """落盘的"上次批过"会变成永久后门。"""
    agent = _build_agent(tmp_path)
    patcher, _ = _with_tty(["a"])

    with patcher:
        agent.run_tool("write_file", {"path": "src/a.txt", "content": "x"})

    assert agent._approval_memory
    saved = list((tmp_path / ".moss" / "sessions").glob("*/meta.json"))
    assert saved
    for path in saved:
        # 按 key 判而不是按整段文本判：tmp_path 的目录名里就带着测试函数名。
        session = json.loads(path.read_text(encoding="utf-8"))
        assert not [key for key in session if "approval" in key], session.keys()


def test_auto_policy_still_skips_the_prompt(tmp_path):
    agent = _build_agent(tmp_path, approval_policy="auto")

    def no_tty(path, *args, **kwargs):
        raise AssertionError("auto approval must not open a tty")

    with patch("builtins.open", no_tty):
        assert agent.approve("write_file", {"path": "a.txt", "content": "x"}) is True
