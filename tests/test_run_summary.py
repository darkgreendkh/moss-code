"""收尾摘要：一次 ask() 跑完，交互层能拿到"改了哪些文件/几步/是否验证"。"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.cli.repl import render_run_summary, _format_token_count


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    # verify_before_final=False：这些用例只验证收尾摘要本身，不想让
    # "改了文件没验证 → 拦一轮" 的逻辑吃掉 FakeModelClient 预置的输出。
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        verify_before_final=False,
    )


def test_summarize_run_reports_changed_files(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"out.txt","content":"hi\\n"}}</tool>',
            "<final>done</final>",
        ],
    )
    agent.ask("write out.txt")
    summary = agent.summarize_run(agent.current_task_state)

    assert summary["changed_files"] == ["out.txt"]
    assert summary["tool_steps"] >= 1
    # 改了文件但没跑验证 —— 收尾必须如实说 unverified，不能谎报。
    assert summary["verified"] is False
    assert summary["status"] == "completed"


def test_summarize_run_marks_verified_after_test_run(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"out.txt","content":"hi\\n"}}</tool>',
            '<tool>{"name":"run_shell","args":{"command":"python -m pytest --version"}}</tool>',
            "<final>done</final>",
        ],
    )
    agent.ask("write then verify")
    summary = agent.summarize_run(agent.current_task_state)

    assert summary["changed_files"] == ["out.txt"]
    assert summary["verified"] is True


def test_run_changed_paths_reset_between_runs(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"a.txt","content":"1\\n"}}</tool>',
            "<final>one</final>",
            '<tool>{"name":"read_file","args":{"path":"a.txt","start":1,"end":1}}</tool>',
            "<final>two</final>",
        ],
    )
    agent.ask("first: write a.txt")
    assert agent.summarize_run(agent.current_task_state)["changed_files"] == ["a.txt"]

    # 第二轮只读不写：上一轮的改动集合不能泄漏过来。
    agent.ask("second: just read")
    assert agent.summarize_run(agent.current_task_state)["changed_files"] == []


def test_render_run_summary_shapes():
    assert _format_token_count(950) == "950"
    assert _format_token_count(4200) == "4.2k"

    text = render_run_summary(
        {
            "status": "completed",
            "tool_steps": 3,
            "changed_files": ["src/a.py", "src/b.py"],
            "verified": True,
            "input_tokens": 4200,
            "output_tokens": 1100,
            "usd": 0.031,
            "usage_estimated": False,
        }
    )
    assert "2 files changed" in text
    assert "verified ✓" in text
    assert "src/a.py" in text and "src/b.py" in text
    assert "$0.031" in text

    # usd=None（不知道价格）时绝不显示成 $0.000。
    no_cost = render_run_summary(
        {"status": "completed", "tool_steps": 1, "changed_files": [], "usd": None}
    )
    assert "$" not in no_cost

    assert render_run_summary({}) == ""


def test_render_run_summary_flags_failure():
    text = render_run_summary(
        {"status": "failed", "stop_reason": "model_error", "tool_steps": 0, "changed_files": []}
    )
    assert "failed (model_error)" in text


def test_render_run_summary_notes_step_limit_stop():
    # 撞步数上限收尾：status=stopped，摘要要如实标"这不是正常完整收尾"。
    text = render_run_summary(
        {
            "status": "stopped",
            "stop_reason": "step_limit_reached",
            "tool_steps": 25,
            "changed_files": [],
        }
    )
    assert "stopped: step limit" in text
    assert "failed" not in text


def test_render_run_summary_stays_quiet_on_normal_finish():
    text = render_run_summary(
        {
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "tool_steps": 3,
            "changed_files": [],
        }
    )
    assert "stopped" not in text and "failed" not in text
