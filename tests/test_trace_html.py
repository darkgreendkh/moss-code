"""本地 trace 可视化（spec-09 §9.6）。

守三条验收：25 步 run 的 HTML <500KB、离线打开正常、**不含任何外部请求**。
"""

import re

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs.observability import events as trace_events
from moss.cli import run_runs_command
from moss.runs.observability.html import BANNER, render_run_html

# 任何会让浏览器去连网的东西。这份页面是排查材料，打开它不该泄露 run_id，
# 也不该在离线时半残。
_EXTERNAL = re.compile(
    r"(https?:)?//|src\s*=\s*[\"']http|@import|url\(\s*[\"']?https?:|<script|integrity=|crossorigin",
    re.I,
)


def _run(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        max_steps=30,
    )
    agent.ask("do the thing")
    run_id = agent.current_task_state.run_id
    return agent, run_id


def _html(tmp_path, outputs):
    agent, run_id = _run(tmp_path, outputs)
    return render_run_html(
        run_id,
        agent.run_store.read_trace(run_id),
        report=agent.run_store.load_report(run_id),
        task_state=agent.run_store.load_task_state(run_id),
    )


# --- 自包含 -------------------------------------------------------------


def test_page_makes_no_external_requests(tmp_path):
    page = _html(tmp_path, ["<final>done</final>"])

    assert not _EXTERNAL.search(page), "页面里出现了会联网的引用"


def test_page_is_self_contained_html(tmp_path):
    page = _html(tmp_path, ["<final>done</final>"])

    assert page.startswith("<!doctype html>")
    assert "<style>" in page and "</html>" in page


def test_header_warns_about_redacted_content(tmp_path):
    """脱敏是尽力而为，工具输出里可能还有内部路径、客户名之类。"""
    assert BANNER in _html(tmp_path, ["<final>done</final>"])


def test_a_twenty_five_step_run_stays_under_500kb(tmp_path):
    outputs = [
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>' if index % 2 == 0
        else '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>'
        for index in range(24)
    ] + ["<final>done</final>"]

    page = _html(tmp_path, outputs)

    assert len(page.encode("utf-8")) < 500_000, len(page)


# --- 内容 ---------------------------------------------------------------


def test_timeline_shows_tool_calls_and_results(tmp_path):
    page = _html(
        tmp_path, ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>done</final>"]
    )

    assert trace_events.TOOL_EXECUTED in page
    assert "list_files" in page
    assert "README.md" in page


def test_failed_tools_are_tagged(tmp_path):
    page = _html(
        tmp_path, ['<tool>{"name":"read_file","args":{"path":"../outside.txt"}}</tool>', "<final>done</final>"]
    )

    assert "invalid_arguments" in page
    assert "is-error" in page


def test_prompt_section_shares_render_as_a_bar(tmp_path):
    page = _html(tmp_path, ["<final>done</final>"])

    assert 'class="bar"' in page
    assert "prefix" in page


def test_summary_carries_run_identity(tmp_path):
    agent, run_id = _run(tmp_path, ["<final>done</final>"])

    page = render_run_html(run_id, agent.run_store.read_trace(run_id), report=agent.run_store.load_report(run_id))

    assert run_id in page
    assert "final_answer_returned" in page


def test_context_health_chart_is_inline_svg(tmp_path):
    page = _html(
        tmp_path,
        ['<tool>{"name":"list_files","args":{"path":"."}}</tool>'] * 3 + ["<final>done</final>"],
    )

    assert "<svg" in page and "polyline" in page


def test_renders_without_a_report_or_task_state(tmp_path):
    """归档过的 run 只剩 trace。排查工具在信息不全时也得能用。"""
    agent, run_id = _run(tmp_path, ["<final>done</final>"])

    page = render_run_html(run_id, agent.run_store.read_trace(run_id))

    assert run_id in page


def test_untrusted_content_is_escaped(tmp_path):
    (tmp_path / "evil.md").write_text("<script>alert(1)</script>\n", encoding="utf-8")
    page = _html(
        tmp_path, ['<tool>{"name":"read_file","args":{"path":"evil.md"}}</tool>', "<final>done</final>"]
    )

    # 工具输出是不可信数据。它进 HTML 之前必须转义，否则这份排查页面
    # 就成了一个由被读文件控制的脚本执行环境。
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- CLI ----------------------------------------------------------------


def test_runs_show_html_writes_a_page(tmp_path, capsys):
    _run(tmp_path, ["<final>done</final>"])
    run_id = next((tmp_path / ".moss" / "runs").glob("run_*")).name

    assert run_runs_command(["show", run_id, "--cwd", str(tmp_path), "--html"]) == 0

    assert capsys.readouterr().out.startswith("<!doctype html>")


def test_runs_show_html_fails_loudly_for_an_unknown_run(tmp_path, capsys):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    assert run_runs_command(["show", "run_nope", "--cwd", str(tmp_path), "--html"]) == 1
    assert "no trace" in capsys.readouterr().err


def test_runs_show_without_html_still_returns_json(tmp_path, capsys):
    _run(tmp_path, ["<final>done</final>"])
    run_id = next((tmp_path / ".moss" / "runs").glob("run_*")).name

    assert run_runs_command(["show", run_id, "--cwd", str(tmp_path)]) == 0

    assert capsys.readouterr().out.lstrip().startswith("{")
