from pathlib import Path

from moss.execution.protocol import ToolContext
import pytest

from moss.execution.registry import BASE_TOOL_SPECS, build_tool_registry, classify_shell_command, native_tool_definitions, tool_delegate, tool_read_file, validate_tool


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
    from moss.execution.registry import tool_edit_file, tool_write_file, write_text_atomic

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
    from moss.execution.registry import ToolField

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


def _file_context(tmp_path):
    return ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )


def test_read_file_omitting_end_honours_the_advertised_schema_default(tmp_path):
    # schema 广告的 default 和 handler 实际用的值曾经漂成 800 vs 200：模型以为一次
    # 读了 800 行，实际只拿到 200 行，剩下的部分它根本不知道存在。
    default_end = BASE_TOOL_SPECS["read_file"].fields["end"].default
    (tmp_path / "long.txt").write_text(
        "".join(f"line {number}\n" for number in range(1, default_end + 51)), encoding="utf-8"
    )

    result = tool_read_file(_file_context(tmp_path), {"path": "long.txt"})

    assert f"line {default_end}\n" in result + "\n"
    assert f"line {default_end + 1}" not in result


def test_read_file_header_reports_the_range_and_total_line_count(tmp_path):
    # 不报总行数的话，模型读完一段既不知道文件还有多长也不知道读到哪了，
    # 只能靠猜下一个区间——同一个文件被反复读就是这么来的。
    (tmp_path / "long.txt").write_text(
        "".join(f"line {number}\n" for number in range(1, 121)), encoding="utf-8"
    )

    result = tool_read_file(_file_context(tmp_path), {"path": "long.txt", "start": 10, "end": 20})

    assert result.splitlines()[0] == "# long.txt (lines 10-20 of 120)"
    # end 超出文件末尾时头部报的是真实末行，不是请求值。
    tail = tool_read_file(_file_context(tmp_path), {"path": "long.txt", "start": 100, "end": 9999})
    assert tail.splitlines()[0] == "# long.txt (lines 100-120 of 120)"


def test_read_file_default_range_stays_under_the_artifact_offload_threshold():
    # 默认值一旦让输出超过 ARTIFACT_THRESHOLD，模型的一次 read_file 就会被卸载成
    # 摘要+指针，得再花一步 read_artifact 才能看到自己刚读的东西。
    import moss
    from moss.execution.executor import ARTIFACT_THRESHOLD

    default_end = BASE_TOOL_SPECS["read_file"].fields["end"].default
    # 用包自身的位置定位源码树，不依赖 pytest 的 cwd。
    for path in Path(moss.__file__).parent.rglob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()[:default_end]
        rendered = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines, start=1))
        assert len(rendered) <= ARTIFACT_THRESHOLD, f"{path} 的默认区间读会触发 artifact 卸载"


def test_classify_shell_command_distinguishes_risk_classes():
    assert classify_shell_command("python -m pytest tests -q").level == "test"
    assert classify_shell_command("git diff --stat").level == "read_only"
    # spec-03 起网络类单独成档，写操作单独成档。
    assert classify_shell_command("git push origin main").level == "network"
    assert classify_shell_command("python scripts/build.py").level == "write"


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

    # 期望值从 ToolSpec 推出来而不是再抄一份字面量：schema 的默认值只能有一个事实源，
    # 测试里抄第二份的话，schema 和 handler 漂开时它照样是绿的。
    spec = BASE_TOOL_SPECS["read_file"]
    assert definitions == [
        {
            "type": "function",
            "name": "read_file",
            "description": spec.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "default": spec.fields["start"].default, "minimum": 1},
                    "end": {"type": "integer", "default": spec.fields["end"].default, "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
