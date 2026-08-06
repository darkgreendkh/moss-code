import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss import cli as moss_cli
from moss.task_state import TaskState


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_workspace_escape_is_rejected(tmp_path):
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "../outside.txt"})

    assert "path escapes workspace" in result


def test_symlink_path_traversal_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "path escapes workspace" in result


def test_risky_tool_deny_behavior(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: approval denied for run_shell"


def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}, clear=True), patch(
        "moss.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = moss_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--approval",
                "auto",
                "--secret-env-name",
                "GITHUB_PAT",
                "--secret-env-name",
                "GH_PAT",
            ]
        )
        agent = moss_cli.build_agent(args)
        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}


def test_cli_build_agent_uses_default_configured_secret_names(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GH_PAT": "ghp-default-1"}, clear=True), patch(
        "moss.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = moss_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = moss_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["GH_PAT"]


def test_cli_build_agent_loads_project_env_secrets_before_redaction_setup(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MOSS_DEEPSEEK_API_KEY=sk-project-secret\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True), patch("moss.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = moss_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])
        agent = moss_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["MOSS_DEEPSEEK_API_KEY"]


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(
        os.environ,
        {
            "MOSS_CUSTOM_SECRET": "custom-secret-value",
            "MOSS_SECRET_ENV_NAMES": "MOSS_CUSTOM_SECRET",
        },
        clear=True,
    ), patch("moss.cli.OllamaModelClient", DummyModelClient):
        args = moss_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = moss_cli.build_agent(args)
        assert agent.secret_env_summary()["secret_env_names"] == ["MOSS_CUSTOM_SECRET"]


def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="auto")
    command = f'"{sys.executable}" -c "import os; print(os.getenv(\'MOSS_ALLOWLIST_SECRET\', \'missing\'))"'

    with patch.dict(os.environ, {"MOSS_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_private_tool_methods_delegate_into_tools_module(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("moss.execution.registry.run_shell_command", return_value=(0, "toolkit-shell\n", "")) as fake_run:
        shell_result = agent._tool_run_shell({"command": "echo bypass", "timeout": 20})

    assert "toolkit-shell" in shell_result
    fake_run.assert_called_once()
    assert agent._tool_run_shell.__func__.__module__ == "moss.runtime"

    with patch("moss.execution.registry.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent._tool_delegate({"task": "inspect README.md", "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()


def test_legacy_public_tool_methods_now_go_through_the_executor(tmp_path):
    """老的公共 tool_* 能整体绕过 ToolExecutor，这正是本次收口要堵的口子。"""
    agent = build_agent(tmp_path, [], approval_policy="never")

    with pytest.deprecated_call():
        result = agent.tool_run_shell({"command": "echo bypass", "timeout": 20})

    # approval=never 时护栏该拦住它；旧实现会直接把命令跑掉。
    assert "approval denied" in result


def test_read_only_agent_cannot_write_through_any_public_api(tmp_path):
    agent = build_agent(tmp_path, [], read_only=True, approval_policy="auto")
    target = tmp_path / "written.txt"

    # read_only 现在由 policy 统一判定（"这次运行不允许 fs_write"），
    # 落到审批之前就被拒了，所以断言只看"被拒了 + 文件没被创建"。
    assert "error:" in agent.run_tool("write_file", {"path": "written.txt", "content": "x"})
    with pytest.deprecated_call():
        assert "error:" in agent.tool_write_file({"path": "written.txt", "content": "x"})
    with pytest.deprecated_call():
        assert "error:" in agent.tool_run_shell({"command": "echo hi > written.txt", "timeout": 5})

    assert not target.exists()


def test_moss_exposes_no_unguarded_tool_entry_points(tmp_path):
    """契约：Moss 上不该再有绕过 executor 的**已定义**公共执行方法。"""
    from moss import Moss as MossClass

    unguarded = [
        name
        for name in vars(MossClass)
        if name.startswith("tool_") and name not in {"tool_signature", "tool_context", "tool_example"}
    ]

    assert unguarded == []


def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"task": "inspect README.md", "max_steps": 2})
    except ValueError as exc:
        assert "delegate depth exceeded" in str(exc)
    else:
        raise AssertionError("delegate depth validation did not fail")


def test_delegate_child_is_read_only(tmp_path):
    target = tmp_path / "child-was-not-allowed.txt"
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"write a file","max_steps":2}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"child-was-not-allowed.txt","content":"nope"}}</tool>',
            "<final>child done</final>",
            "<final>parent done</final>",
        ],
    )

    result = agent.ask("Delegate the work")

    assert result == "parent done"
    assert not target.exists()
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]


def test_configured_secret_env_names_are_redacted_in_trace_and_report(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}, clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Mask configured secrets")
        agent.run_store.start_run(state)

        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}

        payload = {
            "GITHUB_PAT": github_pat,
            "GH_PAT": gh_pat,
            "nested": {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
            "list": [github_pat, gh_pat],
        }
        agent.emit_trace(state, "tool_executed", payload)
        agent.run_store.write_report(
            state,
            agent.redact_artifact({"task_state": state.to_dict(), "payload": payload}),
        )

    run_dir = agent.run_store.run_dir(state.run_id)
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")

    assert github_pat not in trace_text
    assert gh_pat not in trace_text
    assert github_pat not in report_text
    assert gh_pat not in report_text
    assert trace_text.count("<redacted>") >= 4
    assert report_text.count("<redacted>") >= 4


def test_incremental_snapshot_still_rejects_path_escape(tmp_path):
    """快照改成增量之后，路径锚定这条安全线不能跟着松掉。"""
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    agent = build_agent(tmp_path, [])

    agent.capture_workspace_snapshot()

    assert "path escapes workspace" in agent.run_tool("write_file", {"path": "../evil.txt", "content": "x"})
    assert not (tmp_path.parent / "evil.txt").exists()


def test_same_size_rewrite_is_caught_by_the_git_changed_set(tmp_path):
    """(mtime_ns, size) 认不出同尺寸覆盖写，靠 git 变更集兜底。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    target = tmp_path / "same.txt"
    target.write_text("aaaa\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    original = target.stat()

    agent = build_agent(tmp_path, [])
    before = agent.capture_workspace_snapshot()

    target.write_text("bbbb\n", encoding="utf-8")
    # 把 mtime 还原成写入前的样子：这正是 (mtime_ns, size) 的盲区。
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    after = agent.capture_workspace_snapshot()

    changed, summaries = agent.diff_workspace_snapshots(before, after)

    assert changed == ["same.txt"]
    assert summaries == ["modified:same.txt"]


def test_execute_is_a_guarded_structured_entry_point(tmp_path):
    """收口之后外部集成必须有受护栏的入口，否则大家会退回去直连 toolkit。"""
    from moss.execution.protocol import ActionRequest

    agent = build_agent(tmp_path, [], approval_policy="never")

    from_request = agent.execute(ActionRequest(name="run_shell", args={"command": "echo hi", "timeout": 5}))
    from_dict = agent.execute({"name": "run_shell", "args": {"command": "echo hi", "timeout": 5}})

    assert "approval denied" in from_request.content
    assert "approval denied" in from_dict.content


def test_execute_respects_the_allowed_tools_list(tmp_path):
    from moss.execution.protocol import ActionRequest

    agent = build_agent(tmp_path, [], allowed_tools=["read_file"])

    result = agent.execute(ActionRequest(name="write_file", args={"path": "x.txt", "content": "y"}))

    assert "not allowed" in result.content
