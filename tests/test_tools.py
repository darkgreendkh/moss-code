from pathlib import Path

from moss.tool_context import ToolContext
import pytest

from moss.tools import build_tool_registry, classify_shell_command, native_tool_definitions, tool_delegate, tool_read_file, validate_tool


def test_tool_context_supports_file_tools_without_full_moss(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    result = tool_read_file(context, {"path": "sample.txt", "start": 1, "end": 1})

    assert "# sample.txt" in result
    assert "alpha" in result


def test_delegate_uses_context_spawn_without_runtime_import(tmp_path):
    calls = []
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: calls.append(args) or "delegate_result:\nDone",
    )

    result = tool_delegate(context, {"task": "inspect README.md", "max_steps": 2})

    assert result == "delegate_result:\nDone"
    assert calls == [{"task": "inspect README.md", "max_steps": 2}]


def test_build_tool_registry_binds_runners_to_tool_context(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=1,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    tools = build_tool_registry(context)

    assert "read_file" in tools
    assert "delegate" not in tools

def test_write_and_edit_file_leave_no_temp_files(tmp_path):
    from moss.tools import tool_edit_file, tool_write_file, write_text_atomic

    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    write_text_atomic(tmp_path / "direct.txt", "direct\n")
    tool_write_file(context, {"path": "sample.txt", "content": "alpha\n"})
    tool_edit_file(context, {"path": "sample.txt", "old_text": "alpha\n", "new_text": "beta\n"})

    assert (tmp_path / "direct.txt").read_text(encoding="utf-8") == "direct\n"
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "beta\n"
    assert list(tmp_path.glob("sample.txt.*.tmp")) == []


def test_tool_schema_fields_are_executable_contract_objects(tmp_path):
    from moss.tools import ToolField

    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    tools = build_tool_registry(context)
    timeout = tools["run_shell"]["schema"]["timeout"]

    assert isinstance(timeout, ToolField)
    assert timeout.type == "int"
    assert timeout.required is False
    assert timeout.default == 60
    assert timeout.minimum == 1
    assert timeout.maximum == 600


def test_validate_tool_uses_schema_for_required_fields_and_ranges(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    with pytest.raises(ValueError, match="missing required argument: path"):
        validate_tool(context, "read_file", {})
    with pytest.raises(ValueError, match="timeout must be in \\[1, 600\\]"):
        validate_tool(context, "run_shell", {"command": "echo hi", "timeout": 0})


def test_classify_shell_command_distinguishes_risk_classes():
    assert classify_shell_command("python -m pytest tests -q") == "test"
    assert classify_shell_command("git diff --stat") == "read_only"
    assert classify_shell_command("git push origin main") == "destructive_or_network"
    assert classify_shell_command("python scripts/build.py") == "general"


def test_native_tool_definitions_are_generated_from_executable_schema(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=1,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )
    tools = build_tool_registry(context)

    definitions = native_tool_definitions({"read_file": tools["read_file"]}, "openai_responses")

    assert definitions == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a UTF-8 file by line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "default": 1, "minimum": 1},
                    "end": {"type": "integer", "default": 800, "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
