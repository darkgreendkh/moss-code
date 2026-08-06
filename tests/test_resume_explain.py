"""恢复的可解释性、部分恢复与分叉（spec-07 §4.7）。"""

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs import ledger as action_ledger
from moss.runs.observability import events as trace_events
from moss.runs.checkpoint import (
    RESUME_PART_NAMES,
    apply_resume_parts,
    checkpoint_tree,
    explain_resume,
    fork_session,
    parse_resume_parts,
    render_explain,
)
from moss.cli import build_agent, build_arg_parser, main
from moss.runs.store import RunStore
from moss.agent.state import TaskState


def _agent(tmp_path, outputs=()):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )


# ---- --resume-parts ----


def test_parse_resume_parts_defaults_to_everything():
    assert parse_resume_parts(None) == set(RESUME_PART_NAMES)
    assert parse_resume_parts("all") == set(RESUME_PART_NAMES)
    assert parse_resume_parts("memory,history") == {"memory", "history"}


def test_parse_resume_parts_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown resume part"):
        parse_resume_parts("memory,brain")


def test_apply_resume_parts_drops_what_was_not_asked_for():
    session = {
        "id": "s1",
        "history": [{"role": "user", "content": "x"}],
        "memory": {"working": {"recent_files": ["a.py"]}},
        "checkpoints": {"current_id": "c1", "items": {"c1": {"checkpoint_id": "c1", "plan": [{"id": "1"}]}}},
    }

    only_memory = apply_resume_parts(session, {"memory"})

    assert only_memory["history"] == []
    assert only_memory["checkpoints"] == {"current_id": "", "items": {}}
    assert only_memory["memory"]["working"]["recent_files"] == ["a.py"]


def test_apply_resume_parts_can_keep_history_but_drop_the_plan():
    session = {
        "id": "s1",
        "history": [{"role": "user", "content": "x"}],
        "memory": {},
        "checkpoints": {"current_id": "c1", "items": {"c1": {"checkpoint_id": "c1", "plan": [{"id": "1"}]}}},
    }

    trimmed = apply_resume_parts(session, {"history", "checkpoint", "memory"})

    assert trimmed["history"]
    assert trimmed["checkpoints"]["items"]["c1"]["plan"] == []


def test_cli_resume_parts_restores_only_the_requested_pieces(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.ask("first task")
    session_id = agent.session["id"]

    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--approval", "auto", "--resume", session_id, "--resume-parts", "memory"]
    )
    resumed = build_agent(args)

    assert resumed.session["history"] == []
    assert resumed.session["checkpoints"]["items"] == {}


def test_plan_is_restored_on_a_full_resume(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.set_plan([{"id": "1", "title": "read the parser", "status": "in_progress"}])
    agent.ask("first task")
    session_id = agent.session["id"]

    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--approval", "auto", "--resume", session_id]
    )
    resumed = build_agent(args)

    assert [step["title"] for step in resumed.current_plan] == ["read the parser"]


# ---- --fork ----


def test_fork_branches_from_a_checkpoint_and_trims_history():
    session = {
        "id": "s1",
        "workspace_root": "/tmp/repo",
        "history": [{"role": "user", "content": str(index)} for index in range(5)],
        "memory": {"working": {}},
        "checkpoints": {
            "current_id": "c2",
            "items": {
                "c1": {"checkpoint_id": "c1", "parent_checkpoint_id": "", "history_length": 2},
                "c2": {"checkpoint_id": "c2", "parent_checkpoint_id": "c1", "history_length": 5},
            },
        },
    }

    forked = fork_session(session, "c1", "s2")

    assert forked["id"] == "s2"
    assert len(forked["history"]) == 2
    assert forked["checkpoints"]["current_id"] == "c1"
    assert list(forked["checkpoints"]["items"]) == ["c1"]
    assert forked["forked_from"] == {"session_id": "s1", "checkpoint_id": "c1", "inherits_undo": False}


def test_fork_keeps_the_full_ancestry_of_the_branch_point():
    session = {
        "id": "s1",
        "history": [],
        "checkpoints": {
            "current_id": "c3",
            "items": {
                "c1": {"checkpoint_id": "c1", "parent_checkpoint_id": ""},
                "c2": {"checkpoint_id": "c2", "parent_checkpoint_id": "c1"},
                "c3": {"checkpoint_id": "c3", "parent_checkpoint_id": "c2"},
            },
        },
    }

    forked = fork_session(session, "c2", "s2")

    assert list(forked["checkpoints"]["items"]) == ["c1", "c2"]


def test_fork_rejects_an_unknown_checkpoint():
    with pytest.raises(KeyError):
        fork_session({"id": "s1", "history": [], "checkpoints": {"items": {}}}, "nope", "s2")


def test_checkpoint_tree_links_parents_and_children():
    session = {
        "checkpoints": {
            "items": {
                "c1": {"checkpoint_id": "c1", "parent_checkpoint_id": ""},
                "c2": {"checkpoint_id": "c2", "parent_checkpoint_id": "c1"},
                "c3": {"checkpoint_id": "c3", "parent_checkpoint_id": "c1"},
            }
        }
    }

    tree = checkpoint_tree(session)

    assert tree["roots"] == ["c1"]
    assert sorted(tree["children"]["c1"]) == ["c2", "c3"]


def test_cli_fork_leaves_the_original_session_untouched(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.ask("first task")
    session_id = agent.session["id"]
    checkpoint_id = agent.session["checkpoints"]["current_id"]
    before = agent.session_store.load(session_id)

    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--approval", "auto", "--resume", session_id, "--fork", checkpoint_id]
    )
    forked = build_agent(args)

    assert forked.session["id"] != session_id
    assert forked.session["forked_from"]["checkpoint_id"] == checkpoint_id
    # 分叉的意义是"再试一条路"，把原来那条弄坏了就白分叉了。
    assert agent.session_store.load(session_id)["history"] == before["history"]


# ---- --explain ----


def test_explain_reports_freshness_identity_and_pending_actions(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    agent = _agent(
        tmp_path,
        ['<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>', "<final>done</final>"],
    )
    agent.ask("read it")
    session_id = agent.session["id"]

    # 用户在两次运行之间改了文件，并且上次有一个动作没留下回执。
    target.write_text("two\n", encoding="utf-8")
    store = RunStore(tmp_path / ".moss" / "runs", workspace_path=lambda rel: tmp_path / rel)
    crashed = TaskState.create(run_id="run_crashed", task_id="t", user_request="crash")
    store.start_run(crashed)
    store.append_trace(
        crashed,
        {"event": trace_events.ACTION_INTENT, "action_id": "act_1", "tool": "run_shell", "idempotent": False},
    )
    store.lease.release(crashed.run_id)

    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--approval", "auto", "--resume", session_id, "--max-steps", "9"]
    )
    resumed = build_agent(args)
    explanation = explain_resume(resumed)

    assert explanation["session_id"] == session_id
    assert any(item["path"] == "a.txt" and item["stale"] for item in explanation["freshness"])
    assert "max_steps" in [item["field"] for item in explanation["runtime_identity_mismatch"]]
    assert [item["status"] for item in explanation["pending_actions"]] == [
        action_ledger.STATUS_PENDING_UNKNOWN
    ]
    assert explanation["replays_side_effects"] is False


def test_explain_says_when_resuming_would_replay_a_side_effect(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.ask("noop")
    session_id = agent.session["id"]

    store = RunStore(tmp_path / ".moss" / "runs", workspace_path=lambda rel: tmp_path / rel)
    crashed = TaskState.create(run_id="run_crashed", task_id="t", user_request="crash")
    store.start_run(crashed)
    store.append_trace(
        crashed,
        {
            "event": trace_events.ACTION_INTENT,
            "action_id": "act_1",
            "tool": "write_file",
            "idempotent": True,
            "expected_sha": {"a.txt": action_ledger._sha256_text("before")},
            "intended_sha": action_ledger._sha256_text("after"),
        },
    )
    store.lease.release(crashed.run_id)

    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--approval", "auto", "--resume", session_id]
    )
    explanation = explain_resume(build_agent(args))

    assert explanation["replays_side_effects"] is True
    assert "yes" in render_explain(explanation).splitlines()[-1]


def test_render_explain_covers_all_three_questions():
    text = render_explain(
        {
            "session_id": "s1",
            "status": "full-valid",
            "checkpoint_id": "c1",
            "freshness": [{"path": "a.py", "recorded": "1", "current": "2", "stale": True}],
            "stale_paths": ["a.py"],
            "runtime_identity_mismatch": [{"field": "model", "saved": "x", "current": "y"}],
            "pending_actions": [{"run_id": "r1", "tool": "run_shell", "status": "pending_unknown", "reason": "non_idempotent_tool"}],
            "replays_side_effects": False,
        }
    )

    assert "[STALE] a.py: 1 -> 2" in text
    assert "model: 'x' -> 'y'" in text
    assert "run_shell: pending_unknown (non_idempotent_tool)" in text
    assert text.endswith("no")


def test_explain_cli_prints_and_exits_without_running(tmp_path, capsys):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.ask("first task")
    session_id = agent.session["id"]

    code = main(["--cwd", str(tmp_path), "--approval", "auto", "--resume", session_id, "--explain"])

    out = capsys.readouterr().out
    assert code == 0
    assert "resume status:" in out
    assert "resuming will replay a side-effecting action: no" in out


def test_explain_on_a_fresh_session_is_still_readable(tmp_path, capsys):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    code = main(["--cwd", str(tmp_path), "--approval", "auto", "--explain"])

    out = capsys.readouterr().out
    assert code == 0
    assert "(no key files recorded)" in out
