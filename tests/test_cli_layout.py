"""CLI package and tool-registry characterization contracts."""

from pathlib import Path

from moss import cli
from moss.execution.protocol import ToolContext
from moss.execution.registry import build_tool_registry


def test_cli_public_api_is_composed_from_responsibility_modules():
    from moss.cli import factory, parser, repl
    from moss.cli.commands import mcp, memory, runs

    assert cli.build_agent is factory.build_agent
    assert cli.build_arg_parser is parser.build_arg_parser
    assert cli.build_welcome is repl.build_welcome
    assert cli.run_memory_command is memory.run_memory_command
    assert cli.run_runs_command is runs.run_runs_command
    assert cli.run_mcp_command is mcp.run_mcp_command


def test_default_tool_registry_order_is_stable(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda value: Path(tmp_path, value),
        shell_env_provider=dict,
        depth=0,
        max_depth=0,
        spawn_delegate=lambda _args: "",
    )

    assert list(build_tool_registry(context)) == [
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "search_text",
        "update_plan",
        "run_shell",
        "memory_write",
        "memory_update",
        "memory_delete",
        "read_artifact",
        "memory_search",
    ]
