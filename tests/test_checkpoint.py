from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.checkpoint import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_HISTORY_LIMIT,
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_SCHEMA_MISMATCH_STATUS,
    CHECKPOINT_SCHEMA_VERSION,
    create_checkpoint,
    current_checkpoint,
    current_runtime_identity,
    evaluate_resume_state,
)
from moss.run_store import RunStore
from moss.task_state import TaskState


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )


def test_current_runtime_identity_captures_execution_contract(tmp_path):
    agent = build_agent(tmp_path, max_steps=9, max_new_tokens=1024, read_only=True)

    identity = current_runtime_identity(agent)

    assert identity["session_id"] == agent.session["id"]
    assert identity["cwd"] == str(tmp_path)
    assert identity["read_only"] is True
    assert identity["max_steps"] == 9
    assert identity["max_new_tokens"] == 1024
    assert identity["workspace_fingerprint"] == agent.workspace.fingerprint()
    assert identity["tool_signature"] == agent.tool_signature()


def test_evaluate_resume_state_distinguishes_no_checkpoint_full_valid_and_schema_mismatch(tmp_path):
    agent = build_agent(tmp_path)

    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_NONE_STATUS

    identity = current_runtime_identity(agent)
    agent.session["checkpoints"] = {
        "current_id": "ckpt_valid",
        "items": {
            "ckpt_valid": {
                "checkpoint_id": "ckpt_valid",
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "key_files": [],
                "runtime_identity": identity,
            }
        },
    }
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_FULL_VALID_STATUS

    agent.session["checkpoints"]["items"]["ckpt_valid"]["schema_version"] = "old"
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_SCHEMA_MISMATCH_STATUS


def test_create_checkpoint_prunes_old_checkpoints_but_keeps_current(tmp_path):
    agent = build_agent(tmp_path)
    task_state = TaskState.create(task_id="t", user_request="req")

    total = CHECKPOINT_HISTORY_LIMIT + 15
    for index in range(total):
        create_checkpoint(agent, task_state, f"step {index}", trigger="tool_executed")

    items = agent.session["checkpoints"]["items"]
    # 数量被限制住，不再随步数无限增长。
    assert len(items) <= CHECKPOINT_HISTORY_LIMIT
    # 当前 checkpoint 一定还在，恢复链路不受影响。
    assert agent.session["checkpoints"]["current_id"] in items
    assert current_checkpoint(agent) is not None


def test_checkpoint_text_mentions_interrupted_run_and_last_complete_event(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = RunStore(tmp_path / ".moss" / "runs")
    state = TaskState.create(run_id="run_interrupted", task_id="task_interrupted", user_request="Crash.")
    store.start_run(state)
    store.append_trace(state, {"event": "tool_executed"})

    agent = build_agent(tmp_path)

    text = agent.render_checkpoint_text()
    assert "interrupted" in text
    assert "run_interrupted" in text
    assert "tool_executed" in text
