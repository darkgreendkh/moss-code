import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.agent.loop import AgentLoop


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


class RaisingModelClient:
    """A model backend that always fails, to exercise error finalization."""

    def __init__(self, exc):
        self.exc = exc
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.model = "raising-model"

    def complete(self, prompt, max_new_tokens, **kwargs):
        raise self.exc


def build_agent_with_client(tmp_path, client):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=client,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_agent_loop_runs_same_control_flow_as_moss_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.report_path(agent.current_task_state.run_id).exists()


def test_moss_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, ["<final>Facade works.</final>"])

    assert agent.ask("Use facade") == "Facade works."


def test_model_backend_error_finalizes_run_instead_of_crashing(tmp_path):
    agent = build_agent_with_client(
        tmp_path, RaisingModelClient(RuntimeError("Could not reach the backend"))
    )

    answer = agent.ask("Do the task")

    # 后端挂了也要返回一句人能看懂的话，而不是把异常抛给调用方。
    assert "Model backend error" in answer
    assert "Could not reach the backend" in answer

    task_state = agent.current_task_state
    assert task_state.status == "failed"
    assert task_state.stop_reason == "model_error"

    # 关键：即使失败，磁盘上留下的仍是一份完整、可复盘的运行工件。
    report_path = agent.run_store.report_path(task_state.run_id)
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["stop_reason"] == "model_error"

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(task_state.run_id)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(event["event"] == "model_error" for event in trace_events)
    assert any(event["event"] == "run_finished" for event in trace_events)


def test_model_backend_error_message_is_redacted(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    agent = Moss(
        model_client=RaisingModelClient(RuntimeError("bad token sk-secret-value-123456")),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        secret_env_names=["MY_SECRET"],
    )
    import os

    os.environ["MY_SECRET"] = "sk-secret-value-123456"
    try:
        answer = agent.ask("Do the task")
    finally:
        os.environ.pop("MY_SECRET", None)

    assert "sk-secret-value-123456" not in answer
    report_text = agent.run_store.report_path(agent.current_task_state.run_id).read_text(
        encoding="utf-8"
    )
    assert "sk-secret-value-123456" not in report_text
