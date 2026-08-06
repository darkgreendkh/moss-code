"""收尾前自检（spec-02 §4.4）。"""

import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.agent.verification import is_verification_command


def _build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    kwargs.setdefault("max_steps", 10)
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        **kwargs,
    )


def _trace(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]


_WRITE = '<tool>{"name":"write_file","args":{"path":"mod.py","content":"x = 1\\n"}}</tool>'


def test_unverified_edit_is_intercepted_once(tmp_path):
    """未经验证的改动是 agent 最常见的"看起来完成了"，也最贵。"""
    agent = _build_agent(tmp_path, [_WRITE, "<final>Done.</final>", "<final>Done.</final>"])

    assert agent.ask("write it") == "Done."

    events = [event["event"] for event in _trace(agent)]
    assert events.count("verification_requested") == 1
    assert agent.current_task_state.verification_requested is True
    notices = [
        item
        for item in agent.session["history"]
        if item.get("role") == "system" and "never ran a test" in item.get("content", "")
    ]
    assert len(notices) == 1


def test_interception_happens_at_most_once(tmp_path):
    """模型如果坚持不验证，硬顶着不让它收尾只会烧完预算。"""
    agent = _build_agent(
        tmp_path,
        [_WRITE, "<final>Done.</final>", "<final>Done.</final>", "<final>Done.</final>"],
    )

    agent.ask("write it")

    assert [event["event"] for event in _trace(agent)].count("verification_requested") == 1


def test_running_the_tests_skips_the_interception(tmp_path):
    agent = _build_agent(
        tmp_path,
        [
            _WRITE,
            '<tool>{"name":"run_shell","args":{"command":"pytest -q","timeout":20}}</tool>',
            "<final>Done.</final>",
        ],
    )

    assert agent.ask("write and test") == "Done."

    assert "verification_requested" not in [event["event"] for event in _trace(agent)]


def test_read_only_runs_are_never_intercepted(tmp_path):
    agent = _build_agent(
        tmp_path,
        ['<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "<final>Done.</final>"],
    )

    assert agent.ask("just look") == "Done."

    assert "verification_requested" not in [event["event"] for event in _trace(agent)]


def test_the_switch_turns_it_off(tmp_path):
    agent = _build_agent(tmp_path, [_WRITE, "<final>Done.</final>"], verify_before_final=False)

    assert agent.ask("write it") == "Done."

    assert "verification_requested" not in [event["event"] for event in _trace(agent)]


def test_is_verification_command_matches_common_test_runners():
    for command in ("pytest -q", "npm test", "cargo test", "go test ./...", "ruff check ."):
        assert is_verification_command("run_shell", {"command": command}) is True


def test_is_verification_command_looks_at_the_command_not_the_words():
    """`echo "run pytest later"` 不该被当成跑过测试。"""
    assert is_verification_command("run_shell", {"command": 'echo "run pytest later"'}) is False


def test_is_verification_command_handles_chained_commands():
    assert is_verification_command("run_shell", {"command": "cd sub && pytest -q"}) is True


def test_only_shell_commands_count_as_verification():
    assert is_verification_command("read_file", {"path": "test_mod.py"}) is False


def test_shell_risk_class_is_reused_when_available():
    assert is_verification_command("run_shell", {"command": "unknown-runner"}, {"shell_risk_class": "test"}) is True


def test_no_verification_tool_means_no_interception(tmp_path):
    """没给 run_shell 的运行里要求"先去验证"，只会白烧一轮——模型也做不到。"""
    agent = _build_agent(
        tmp_path,
        [_WRITE, "<final>Done.</final>"],
        allowed_tools=["read_file", "write_file"],
    )

    assert agent.ask("write it") == "Done."

    assert "verification_requested" not in [event["event"] for event in _trace(agent)]
