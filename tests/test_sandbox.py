"""分层沙箱（spec-03 §4.6）。

核心断言不是"隔离一定生效"（各平台能力不同），而是**降级必须看得见**：
用户以为自己开了沙箱、实际没开，是最危险的状态。
"""

from unittest.mock import patch

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.agent.state import TaskState
from moss.execution.safety.sandbox import SandboxPlan, detect, wrap_command
from moss.execution.safety.shell import extract_hosts, host_allowed


def test_off_is_not_reported_as_degraded():
    plan = detect("off")

    assert plan.mode == "none"
    assert plan.degraded is False


def test_auto_uses_sandbox_exec_on_macos():
    with patch("shutil.which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None):
        plan = detect("auto", platform="darwin")

    assert plan.mode == "sandbox-exec"
    assert plan.degraded is False


def test_auto_uses_bwrap_on_linux():
    with patch("shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None):
        plan = detect("auto", platform="linux")

    assert plan.mode == "bwrap"


def test_missing_tool_for_an_explicit_request_is_a_visible_degradation():
    with patch("shutil.which", lambda name: None):
        plan = detect("bwrap", platform="linux")

    assert plan.mode == "none"
    assert plan.degraded is True
    assert "bwrap not found" in plan.reason


def test_auto_without_any_sandbox_is_not_flagged_as_degraded():
    with patch("shutil.which", lambda name: None):
        plan = detect("auto", platform="linux")

    assert plan.mode == "none"
    # auto 的语义就是"有就用"，没有不算降级，但原因仍然记下来。
    assert plan.degraded is False
    assert plan.reason


def test_an_unusable_container_is_an_error_not_a_silent_fallback():
    """用户显式要了容器却退回宿主机执行，等于把隔离意图丢掉。"""
    with patch("shutil.which", lambda name: None), pytest.raises(RuntimeError, match="docker"):
        detect("docker")


def test_wrap_command_limits_writes_to_the_workspace():
    macos = wrap_command("pytest -q", SandboxPlan(mode="sandbox-exec"), workspace="/repo")
    linux = wrap_command("pytest -q", SandboxPlan(mode="bwrap"), workspace="/repo")

    assert macos[0] == "sandbox-exec"
    assert '(subpath "/repo")' in macos[2]
    assert "deny file-write*" in macos[2]
    assert linux[0] == "bwrap"
    assert "--ro-bind" in linux and "--bind" in linux


def test_containers_run_without_network_and_without_root():
    argv = wrap_command("pytest -q", SandboxPlan(mode="docker"), workspace="/repo")

    assert "--network=none" in argv
    assert "--user" in argv


def test_no_sandbox_means_the_command_is_untouched():
    assert wrap_command("ls", SandboxPlan(mode="none"), workspace="/repo") is None


def test_degradation_is_announced_on_stderr(capsys):
    from moss.execution.safety.sandbox import announce

    announce(SandboxPlan(mode="none", requested="bwrap", degraded=True, reason="bwrap not found"))

    assert "sandbox=none" in capsys.readouterr().err


def _build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    kwargs.setdefault("sandbox", "off")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        **kwargs,
    )


def test_sandbox_state_reaches_the_report(tmp_path):
    agent = _build_agent(tmp_path)

    report = agent.build_report(TaskState.create(task_id="t", user_request="x"))

    assert report["sandbox"]["mode"] == "none"
    assert report["sandbox"]["requested"] == "off"


def test_network_commands_are_asked_about_even_under_auto_approval(tmp_path):
    """网络类是唯一一类"做错了收不回来"的操作：数据已经送出去了。"""
    agent = _build_agent(tmp_path)
    asked = []
    agent._ask_for_approval = lambda name, args: asked.append(args["command"]) or False

    result = agent.run_tool("run_shell", {"command": "curl https://example.com", "timeout": 5})

    assert asked == ["curl https://example.com"]
    assert "approval denied" in result


def test_local_commands_are_still_auto_approved(tmp_path):
    agent = _build_agent(tmp_path)
    agent._ask_for_approval = lambda name, args: pytest.fail("local commands must not prompt under auto")

    assert "exit_code: 0" in agent.run_tool("run_shell", {"command": "echo hi", "timeout": 5})


def test_hosts_outside_the_allowlist_are_refused(tmp_path):
    agent = _build_agent(tmp_path, allowed_network_hosts=("example.com",))

    allowed = agent.network_hosts_refused("run_shell", {"command": "curl https://api.example.com/x"})
    refused = agent.network_hosts_refused("run_shell", {"command": "curl https://evil.test/x"})

    assert allowed == ()
    assert refused == ("evil.test",)
    assert "approval denied" in agent.run_tool(
        "run_shell", {"command": "curl https://evil.test/x", "timeout": 5}
    )


def test_extract_hosts_reads_literal_domains():
    assert extract_hosts("curl https://example.com/x") == ("example.com",)
    assert extract_hosts("wget http://a.b.co/x && curl https://c.dev/y") == ("a.b.co", "c.dev")
    # `curl $URL` 拿不到域名——那种命令已经因为命令替换被判 high，由审批兜底。
    assert extract_hosts("curl $URL") == ()


def test_host_allowed_matches_subdomains():
    assert host_allowed("api.example.com", ("example.com",)) is True
    assert host_allowed("example.com", ("example.com",)) is True
    assert host_allowed("notexample.com", ("example.com",)) is False
