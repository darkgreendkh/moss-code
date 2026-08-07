"""过程可见性：成功的工具也向进度渲染器交代它看见/改动了什么。"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.cli.repl import format_tool_result


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        verify_before_final=False,
    )


def test_format_tool_result_shapes():
    # 结果行带上工具名，方便和上面的调用行归位。
    assert format_tool_result({"name": "run_shell", "status": "ok", "exit_code": 0}) == "      → run_shell  exit 0"
    assert "(error)" in format_tool_result({"name": "run_shell", "status": "error", "exit_code": 1})
    assert "+1 new" in format_tool_result(
        {"name": "write_file", "status": "ok", "diff_summary": ["created:a.py"]}
    )
    assert "~2 changed" in format_tool_result(
        {"name": "edit_file", "status": "ok", "diff_summary": ["modified:a.py", "modified:b.py"]}
    )
    # 没有结构化信号时退回结果头一行。
    assert "lines 1-40" in format_tool_result(
        {"name": "read_file", "status": "ok", "preview": "# foo.py (lines 1-40 of 120)"}
    )
    # 纯无副作用、无输出的工具不强行占一行。
    assert format_tool_result({"name": "update_plan", "status": "ok"}) == ""


def test_result_lines_name_their_tool_so_grouped_batches_dont_confuse():
    # 回归：一个 batch 里 read_file 的结果和 list_files 的结果被分组打在一起时，
    # 每条结果都必须标明是哪个工具产出的——否则 `# CLAUDE.md (lines…)` 会被误读成
    # list_files 打印了行号范围。list_files 自己从不带行号范围。
    read_line = format_tool_result(
        {"name": "read_file", "status": "ok", "preview": "# CLAUDE.md (lines 1-298 of 298)"}
    )
    list_line = format_tool_result(
        {"name": "list_files", "status": "ok", "preview": "[D] docs/decisions"}
    )
    assert read_line.startswith("      → read_file  ")
    assert "lines 1-298 of 298" in read_line
    assert list_line.startswith("      → list_files  ")
    assert "lines" not in list_line  # 列目录不带行号范围


def test_tool_result_events_carry_visible_detail(tmp_path):
    events = []
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"out.py","content":"x=1\\n"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"out.py","start":1,"end":1}}</tool>',
            "<final>done</final>",
        ],
    )
    agent.progress_observer = lambda event, payload: events.append((event, payload))
    agent.ask("write then read")

    results = [payload for event, payload in events if event == "tool_result"]
    write_result = next(r for r in results if r["name"] == "write_file")
    read_result = next(r for r in results if r["name"] == "read_file")

    # 写文件带上了 diff 摘要，渲染出来能看出"改了什么"。
    assert any(item.startswith(("created:", "modified:")) for item in write_result["diff_summary"])
    assert "+1 new" in format_tool_result(write_result) or "~1 changed" in format_tool_result(write_result)
    # 读文件带上了结果头一行（含总行数）。
    assert "of 1" in read_result["preview"]
