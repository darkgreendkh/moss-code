from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.execution.executor import ToolExecutor, ToolExecutionResult


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_tool_executor_returns_content_and_metadata_without_side_channel(tmp_path):
    agent = build_agent(tmp_path)

    result = ToolExecutor(agent).execute("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert isinstance(result, ToolExecutionResult)
    assert "# README.md" in result.content
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["read_only"] is True
    assert result.metadata["workspace_changed"] is False


def test_moss_run_tool_keeps_compatibility_metadata(tmp_path):
    agent = build_agent(tmp_path)

    content = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert "# README.md" in content
    assert agent._last_tool_result_metadata["tool_status"] == "ok"

def test_run_shell_uses_structured_exit_code_when_rendered_text_has_no_exit_code(tmp_path):
    from moss.execution.registry import ToolRunOutput

    agent = build_agent(tmp_path)
    agent.tools["run_shell"]["run"] = lambda args: ToolRunOutput(
        content="stderr:\nboom",
        stdout="",
        stderr="boom",
        exit_code=7,
    )

    result = ToolExecutor(agent).execute("run_shell", {"command": "custom command"})

    assert result.content == "stderr:\nboom"
    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert result.metadata["exit_code"] == 7
    assert result.metadata["stdout_chars"] == 0
    assert result.metadata["stderr_chars"] == 4


def test_run_shell_structured_exit_code_with_workspace_change_is_partial_success(tmp_path):
    from moss.execution.registry import ToolRunOutput

    agent = build_agent(tmp_path)

    def run_shell(args):
        (tmp_path / "created.txt").write_text("changed\n", encoding="utf-8")
        return ToolRunOutput(content="changed workspace", exit_code=1)

    agent.tools["run_shell"]["run"] = run_shell

    result = ToolExecutor(agent).execute("run_shell", {"command": "custom command"})

    assert result.metadata["tool_status"] == "partial_success"
    assert result.metadata["tool_error_code"] == "tool_partial_success"
    assert result.metadata["exit_code"] == 1
    assert result.metadata["workspace_changed"] is True
    assert "created.txt" in result.metadata["affected_paths"]
