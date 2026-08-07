"""会话内运行时命令：/approval /verify /model /config 就地改设置并立即生效。"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.cli.repl import (
    apply_approval,
    apply_model,
    apply_verify,
    render_approvals,
    render_config,
)
from moss.execution.safety.injection import InjectionFinding


def build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=workspace,
        session_store=store,
        **kwargs,
    )


def test_approval_shows_and_switches(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    assert "ask" in apply_approval(agent, "")
    apply_approval(agent, "auto")
    assert agent.approval_policy == "auto"
    # 非法值不改变现状。
    apply_approval(agent, "loose")
    assert agent.approval_policy == "auto"


def test_verify_toggles(tmp_path):
    agent = build_agent(tmp_path, verify_before_final=True)
    apply_verify(agent, "off")
    assert agent.verify_before_final is False
    apply_verify(agent, "on")
    assert agent.verify_before_final is True


def test_model_switches_live(tmp_path):
    agent = build_agent(tmp_path)
    before = agent.model_client.model
    msg = apply_model(agent, "some-other-model")
    assert agent.model_client.model == "some-other-model"
    assert before in msg


def test_config_lists_current_settings(tmp_path):
    agent = build_agent(tmp_path, approval_policy="never")
    text = render_config(agent)
    assert "approval" in text and "never" in text
    assert "model" in text


def test_approval_prompt_puts_diff_on_its_own_lines(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    prompt = agent._approval_prompt("write_file", {"path": "a.txt", "content": "new\n"})
    lines = prompt.splitlines()
    # 回答提示必须独占最后一行，不再和 diff 挤在一起。
    assert lines[-1].startswith("[y = once")
    assert lines[0] == "approve write_file?"
    # diff 内容缩进成一块。
    assert any(line.strip().startswith("-old") for line in lines)


def test_read_approval_answer_gives_readline_only_a_short_prompt(tmp_path, monkeypatch):
    # 回归：多行 + 宽字符(·)的图例整块喂给 input()，GNU readline 会横向滚动把左侧藏掉，
    # 用户只看到 "d = never · N = no"，误以为只有这两个选项。图例必须当正文打完，
    # input() 只拿一个极短 ASCII 提示。这里逼走 /dev/tty 分支去验证 input() 那条路。
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr("moss.execution.service.sys.stdin", _Tty())
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(a[0] if a else ""))
    seen = {}

    def fake_input(prompt):
        seen["prompt"] = prompt
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)

    agent = build_agent(tmp_path, approval_policy="ask")
    assert agent._read_approval_answer(agent._approval_prompt("run_shell", {"command": "ls"})) == "y"
    # 真正的输入提示是两个字符，绝不含换行或宽字符 —— readline 怎么都算不错。
    assert seen["prompt"] == "> "
    # 完整图例(含 y/a 两个选项)作为正文打给了用户，一个都没被藏掉。
    banner = "\n".join(str(p) for p in printed)
    assert "y = once" in banner and "a = always" in banner


def test_approval_prompt_explains_injection_reason(tmp_path):
    agent = build_agent(tmp_path, approval_policy="auto")
    agent.flag_injection_suspected(
        InjectionFinding(pattern="ignore_previous_instructions", excerpt="x", score=9, source="tool")
    )
    prompt = agent._approval_prompt("run_shell", {"command": "curl http://x"})
    assert "prompt-injection suspected" in prompt
    assert "ignore previous instructions" in prompt


def test_approvals_view_and_clear(tmp_path):
    agent = build_agent(tmp_path)
    assert "no remembered" in render_approvals(agent, "")
    agent._approval_memory[("run_shell", "high", "rm")] = True
    agent._approval_memory[("write_file", "high", "docs")] = False
    view = render_approvals(agent, "")
    assert "always allow" in view and "run_shell" in view
    assert "always deny" in view and "write_file" in view
    assert "cleared 2" in render_approvals(agent, "clear")
    assert agent.remembered_approvals() == {}
    assert "usage:" in render_approvals(agent, "bogus")
