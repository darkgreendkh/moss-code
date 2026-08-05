from moss.task_state import (
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_RETRY_LIMIT_REACHED,
    STOP_REASON_STEP_LIMIT_REACHED,
    TaskState,
)


def test_task_state_starts_running_with_empty_progress():
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    assert state.task_id == "task_001"
    assert state.run_id == "run_001"
    assert state.user_request == "Inspect the repo."
    assert state.status == "running"
    assert state.tool_steps == 0
    assert state.attempts == 0
    assert state.last_tool == ""
    assert state.stop_reason == ""
    assert state.final_answer == ""


def test_task_state_records_success_and_final_answer():
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Fix the bug.")
    state.record_attempt()
    state.record_tool("read_file")
    state.finish_success("Done.")

    assert state.attempts == 1
    assert state.tool_steps == 1
    assert state.last_tool == "read_file"
    assert state.status == "completed"
    assert state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
    assert state.final_answer == "Done."


def test_task_state_records_step_limit_stop_reason():
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Try again.")

    state.stop_step_limit()

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_STEP_LIMIT_REACHED


def test_task_state_records_retry_limit_stop_reason():
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Try again.")

    state.stop_retry_limit()

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_RETRY_LIMIT_REACHED


def test_task_state_snapshot_keeps_final_answer():
    state = TaskState.create(run_id="run_005", task_id="task_005", user_request="Return the answer.")
    state.finish_success("Final answer.")

    snapshot = state.to_dict()

    assert snapshot["final_answer"] == "Final answer."
    assert snapshot["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED


def test_task_state_snapshot_keeps_checkpoint_reference_without_body():
    state = TaskState.create(run_id="run_006", task_id="task_006", user_request="Resume the task.")
    state.checkpoint_id = "ckpt_001"
    state.resume_status = "full-valid"

    snapshot = state.to_dict()

    assert snapshot["checkpoint_id"] == "ckpt_001"
    assert snapshot["resume_status"] == "full-valid"
    assert "current_goal" not in snapshot
    assert "next_step" not in snapshot


def test_model_turns_and_tool_calls_are_separate_from_tool_steps():
    """三个口径各回答一个问题，不能互相顶替。"""
    state = TaskState.create(task_id="t", user_request="x")

    state.record_model_turn()
    state.record_tool_call("read_file")
    state.record_tool("read_file")
    state.record_model_turn()
    state.record_tool_call("write_file")

    assert state.model_turns == 2
    # tool_calls 含没进入执行阶段的调用，所以 >= tool_steps。
    assert state.tool_calls == 2
    assert state.tool_steps == 1


def test_old_task_state_json_without_the_new_fields_still_loads():
    state = TaskState.from_dict(
        {"run_id": "r", "task_id": "t", "user_request": "x", "tool_steps": 3}
    )

    assert state.model_turns == 0
    assert state.tool_calls == 0
    assert state.verification_requested is False
    assert state.tool_steps == 3
