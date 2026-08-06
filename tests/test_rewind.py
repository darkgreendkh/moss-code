"""`/rewind`：文件 + 历史 + memory 一起回退（spec-07 §4.9）。

最重要的一条：**用户自己的未提交改动不得被覆盖**。悄悄盖掉用户手改的内容，
是这个功能唯一不可接受的失败方式。
"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs.rewind import (
    STATUS_NEEDS_CONFIRMATION,
    STATUS_NOTHING_TO_DO,
    STATUS_OK,
    plan_rewind,
    render_rewind,
)


def _agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )


def _write(path, content):
    return f'<tool>{{"name":"write_file","args":{{"path":"{path}","content":"{content}"}}}}</tool>'


def test_rewind_restores_the_file_byte_for_byte(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = _agent(tmp_path, [_write("a.txt", "changed"), "<final>done</final>"])
    agent.ask("change it")
    assert target.read_text(encoding="utf-8") == "changed"

    result = agent.rewind()

    assert result["status"] == STATUS_OK
    assert target.read_text(encoding="utf-8") == "original\n"
    assert result["restored"] == [{"path": "a.txt", "action": "restored"}]


def test_rewind_deletes_a_file_the_agent_created(tmp_path):
    agent = _agent(tmp_path, [_write("new.txt", "hello"), "<final>done</final>"])
    agent.ask("create it")
    assert (tmp_path / "new.txt").exists()

    agent.rewind()

    # 那次动作之前这个文件不存在，回滚就是把它删掉。
    assert not (tmp_path / "new.txt").exists()


def test_rewind_truncates_history_and_rolls_back_memory(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = _agent(tmp_path, [_write("a.txt", "changed"), "<final>done</final>"])
    agent.ask("change it")
    history_after = len(agent.session["history"])

    result = agent.rewind()

    assert result["status"] == STATUS_OK
    assert len(agent.session["history"]) < history_after
    # 只回退文件而留着"我已经改好了"的历史，下一轮就会在错误前提上继续。
    assert "changed" not in agent.history_text()


def test_rewinding_two_steps_rolls_memory_back_to_the_first_checkpoint(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("v0\n", encoding="utf-8")
    agent = _agent(
        tmp_path,
        [_write("a.txt", "v1"), _write("b.txt", "v2"), "<final>done</final>"],
    )
    agent.ask("change two files")
    assert "b.txt" in agent.memory.to_dict()["working"]["recent_files"]

    result = agent.rewind(steps=1)

    # 回到第一步之后的 checkpoint：那时候 b.txt 还没被碰过。
    assert result["status"] == STATUS_OK
    assert result["checkpoint_id"] == agent.session["checkpoints"]["current_id"]
    assert "b.txt" not in agent.memory.to_dict()["working"]["recent_files"]


def test_rewind_refuses_to_overwrite_a_change_the_user_made(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = _agent(tmp_path, [_write("a.txt", "agent version"), "<final>done</final>"])
    agent.ask("change it")
    # 用户在 agent 改完之后又手动改了一遍。
    target.write_text("my own edit\n", encoding="utf-8")

    result = agent.rewind()

    assert result["status"] == STATUS_NEEDS_CONFIRMATION
    assert result["conflicts"][0]["path"] == "a.txt"
    assert result["conflicts"][0]["reason"] == "changed_since_run"
    # 什么都没做：部分回滚会留下一个"一半旧一半新"的工作区，比不回滚更难收拾。
    assert target.read_text(encoding="utf-8") == "my own edit\n"
    assert agent.session["undo"]


def test_forced_rewind_discards_the_user_change_after_confirmation(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = _agent(tmp_path, [_write("a.txt", "agent version"), "<final>done</final>"])
    agent.ask("change it")
    target.write_text("my own edit\n", encoding="utf-8")

    result = agent.rewind(force=True)

    assert result["status"] == STATUS_OK
    assert target.read_text(encoding="utf-8") == "original\n"


def test_rewind_multiple_steps_replays_backwards(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("v0\n", encoding="utf-8")
    agent = _agent(
        tmp_path,
        [_write("a.txt", "v1"), _write("a.txt", "v2"), "<final>done</final>"],
    )
    agent.ask("change it twice")
    assert target.read_text(encoding="utf-8") == "v2"

    agent.rewind(steps=2)

    # 倒着回放：后发生的动作先撤，才能回到最早那一步之前的样子。
    assert target.read_text(encoding="utf-8") == "v0\n"


def test_rewind_one_step_of_two_lands_in_the_middle(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("v0\n", encoding="utf-8")
    agent = _agent(
        tmp_path,
        [_write("a.txt", "v1"), _write("a.txt", "v2"), "<final>done</final>"],
    )
    agent.ask("change it twice")

    agent.rewind(steps=1)

    assert target.read_text(encoding="utf-8") == "v1"
    assert len(agent.session["undo"]) == 1


def test_rewind_without_any_recorded_change_is_a_no_op(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.ask("do nothing")

    result = agent.rewind()

    assert result["status"] == STATUS_NOTHING_TO_DO
    assert "nothing to rewind" in render_rewind(result)


def test_shell_steps_are_reported_as_not_restorable(tmp_path):
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"echo hi > out.txt"}}</tool>', "<final>done</final>"],
    )
    agent.ask("run it")

    _, conflicts = plan_rewind(agent, 1)

    # run_shell 改了什么要执行完才知道，逐文件备份来不及 —— 说清楚而不是假装能撤销。
    assert conflicts and conflicts[0]["reason"] == "not_restorable"
    assert agent.rewind()["status"] == STATUS_NEEDS_CONFIRMATION


def test_render_rewind_explains_the_conflict(tmp_path):
    text = render_rewind(
        {
            "status": STATUS_NEEDS_CONFIRMATION,
            "conflicts": [
                {"path": "a.txt", "reason": "changed_since_run"},
                {"path": "", "reason": "not_restorable", "tool": "run_shell", "git_object": "abc123def456789"},
            ],
        }
    )

    assert "a.txt: changed_since_run" in text
    assert "run_shell cannot be undone automatically (git object abc123def456)" in text
    assert "/rewind!" in text


def test_undo_records_land_in_the_run_directory(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = _agent(tmp_path, [_write("a.txt", "changed"), "<final>done</final>"])
    agent.ask("change it")

    entry = agent.session["undo"][-1]
    manifest = agent.run_store.read_undo(entry["run_id"], entry["action_id"])

    assert manifest["tool"] == "write_file"
    assert manifest["paths"] == ["a.txt"]
    assert manifest["restorable"] is True
    assert manifest["sha_after"]["a.txt"]
    assert (
        agent.run_store.undo_file_text(entry["run_id"], entry["action_id"], "a.txt") == "original\n"
    )


def test_rewind_survives_a_reloaded_session(tmp_path):
    """回滚记录挂在 run 目录里，重启一个进程照样能用。"""
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    agent = _agent(tmp_path, [_write("a.txt", "changed"), "<final>done</final>"])
    agent.ask("change it")
    session_id = agent.session["id"]

    reopened = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        session=SessionStore(tmp_path / ".moss" / "sessions").load(session_id),
        approval_policy="auto",
    )
    result = reopened.rewind()

    assert result["status"] == STATUS_OK
    assert target.read_text(encoding="utf-8") == "original\n"
