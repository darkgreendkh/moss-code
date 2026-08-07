"""撞步数上限时：先提醒收敛，再不行就强制作答，绝不甩一句 stopped 就跑。"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs.observability import events as trace_events


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    kwargs.setdefault("approval_policy", "auto")
    kwargs.setdefault("verify_before_final", False)
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        **kwargs,
    )


def _read(path="README.md"):
    return f'<tool>{{"name":"read_file","args":{{"path":"{path}","start":1,"end":1}}}}</tool>'


def test_step_limit_forces_a_final_answer_instead_of_canned_stop(tmp_path):
    # max_steps=2：两次读文件耗尽步数，第 3 个输出留给"强制作答"这一轮。
    agent = build_agent(
        tmp_path,
        [_read(), _read(), "<final>Here is my best answer from what I gathered.</final>"],
        max_steps=2,
    )
    answer = agent.ask("open-ended question")

    assert "best answer" in answer
    # 语义仍是撞了步数上限，但现在带着一个真答案，而不是 "Stopped...".
    assert agent.current_task_state.stop_reason == "step_limit_reached"
    assert "Stopped after reaching the step limit" not in answer


def test_step_limit_falls_back_to_informative_summary_when_synthesis_unavailable(tmp_path):
    # 只给两次读文件、不给收尾那一轮的输出：合成调用会 RuntimeError→退回规则总结。
    agent = build_agent(tmp_path, [_read(), _read()], max_steps=2)
    answer = agent.ask("open-ended question")

    assert agent.current_task_state.stop_reason == "step_limit_reached"
    # 退回的总结要有信息量：点明步数上限 + 怎么继续，而不是空洞一句。
    assert "step limit" in answer
    assert "--max-steps" in answer


def test_convergence_nudge_fires_once_near_the_step_ceiling(tmp_path):
    events = []
    # max_steps=12（过了 CONVERGE_MIN_STEPS）→ 阈值 int(12*0.8)=9：第 9 步起提醒一次。
    agent = build_agent(
        tmp_path,
        [_read()] * 9 + ["<final>done</final>"],
        max_steps=12,
    )
    original_emit = agent.emit_trace

    def spy(task_state, event, payload=None):
        events.append(event)
        return original_emit(task_state, event, payload)

    agent.emit_trace = spy
    agent.ask("keep reading")

    assert events.count(trace_events.CONVERGENCE_NUDGE) == 1
    # 收敛提醒进了 history，让模型下一步能据此改主意。
    system_notes = [
        item["content"]
        for item in agent.session["history"]
        if item.get("role") == "system" and "tool steps" in item.get("content", "")
    ]
    assert any("converge" in note.lower() for note in system_notes)


def test_convergence_nudge_stays_silent_on_tight_budgets(tmp_path):
    # 预算很紧（< CONVERGE_MIN_STEPS）时不该插收敛提醒——上限自己就是压力。
    events = []
    agent = build_agent(tmp_path, [_read(), _read(), _read(), "<final>done</final>"], max_steps=5)
    original_emit = agent.emit_trace

    def spy(task_state, event, payload=None):
        events.append(event)
        return original_emit(task_state, event, payload)

    agent.emit_trace = spy
    agent.ask("short task")
    assert trace_events.CONVERGENCE_NUDGE not in events
