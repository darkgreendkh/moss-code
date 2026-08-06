"""大输出卸载到 artifact + read_artifact 取回（spec-06 §4.2）。

硬截断是有损的：砍掉的行既不进摘要也不落盘，而失败原因偏偏常在那里面。
这些测试守的是"完整输出一定在盘上、指针形状对、越界读被拒、同内容只占一份盘"。
"""

import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss import tools as toolkit
from moss.tool_executor import ARTIFACT_THRESHOLD


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def _big_file(tmp_path, name="big.txt", lines=900):
    text = "\n".join(f"line {index} " + "z" * 40 for index in range(lines))
    (tmp_path / name).write_text(text, encoding="utf-8")
    return text


def test_large_output_is_offloaded_with_a_pointer(tmp_path):
    _big_file(tmp_path)
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"big.txt","start":1,"end":900}}</tool>',
            "<final>done</final>",
        ],
    )

    agent.ask("read the big file")

    tool_entry = next(item for item in agent.session["history"] if item.get("role") == "tool")
    assert tool_entry["artifact"].startswith("artifacts/")
    assert tool_entry["artifact_lines"] > 800
    assert "read_artifact(" in tool_entry["content"]
    assert len(tool_entry["content"]) < ARTIFACT_THRESHOLD * 2

    run_dir = agent.run_store.run_dir(agent.current_task_state.run_id)
    artifact_path = run_dir / tool_entry["artifact"]
    assert artifact_path.exists()
    assert artifact_path.read_text(encoding="utf-8").splitlines()[-1].endswith("z" * 40)


def test_offloaded_output_loses_no_bytes(tmp_path):
    _big_file(tmp_path, lines=2000)
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"big.txt","start":1,"end":2000}}</tool>',
            "<final>done</final>",
        ],
    )

    agent.ask("read the big file")

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["truncated_bytes_lost"] == 0
    tool_event = next(
        event
        for event in agent.run_store.read_trace(agent.current_task_state.run_id)
        if event["event"] == "tool_executed"
    )
    assert tool_event["truncated_bytes_lost"] == 0
    assert tool_event["artifact_path"].startswith("artifacts/")


def test_identical_output_is_stored_once(tmp_path):
    _big_file(tmp_path)
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"big.txt","start":1,"end":900}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"big.txt","start":1,"end":901}}</tool>',
            "<final>done</final>",
        ],
        max_steps=4,
    )

    agent.ask("read it twice")

    run_dir = agent.run_store.run_dir(agent.current_task_state.run_id)
    stored = list((run_dir / "artifacts").glob("*.txt"))
    assert len(stored) == 1
    pointers = {
        item["artifact"] for item in agent.session["history"] if item.get("role") == "tool" and item.get("artifact")
    }
    assert len(pointers) == 1


def test_read_artifact_returns_the_requested_line_range(tmp_path):
    _big_file(tmp_path)
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"big.txt","start":1,"end":900}}</tool>',
            "<final>placeholder</final>",
        ],
    )
    agent.ask("read the big file")
    pointer = next(item["artifact"] for item in agent.session["history"] if item.get("artifact"))

    result = agent.run_tool("read_artifact", {"path": pointer, "start": 5, "end": 7})

    # artifact 存的是 read_file 的原始输出（本身带行号），所以第 5 行是 "line 3"。
    assert "line 3 " in result
    assert "line 5 " in result
    assert "line 6 " not in result
    assert "lines 5-7 of 901" in result


def test_read_artifact_refuses_to_escape_the_run_directory(tmp_path):
    (tmp_path / "secret.txt").write_text("top secret\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>done</final>"])
    agent.ask("start a run so the run directory exists")

    escaped = agent.run_tool("read_artifact", {"path": "../../../secret.txt"})
    absolute = agent.run_tool("read_artifact", {"path": str(tmp_path / "secret.txt")})

    assert "path escapes run directory" in escaped
    assert "top secret" not in escaped
    assert "path escapes run directory" in absolute
    assert "top secret" not in absolute


def test_artifact_content_is_redacted_before_it_lands_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_TEST_TOKEN", "sk-live-abcdef0123456789")
    body = "\n".join(f"line {index} sk-live-abcdef0123456789" for index in range(400))
    (tmp_path / "leaky.txt").write_text(body, encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"leaky.txt","start":1,"end":400}}</tool>',
            "<final>done</final>",
        ],
        secret_env_names=["MOSS_TEST_TOKEN"],
    )

    agent.ask("read the leaky file")

    run_dir = agent.run_store.run_dir(agent.current_task_state.run_id)
    stored = next((run_dir / "artifacts").glob("*.txt"))
    assert "sk-live-abcdef0123456789" not in stored.read_text(encoding="utf-8")


def test_small_output_is_not_offloaded(tmp_path):
    (tmp_path / "small.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"small.txt","start":1,"end":10}}</tool>',
            "<final>done</final>",
        ],
    )

    agent.ask("read the small file")

    tool_entry = next(item for item in agent.session["history"] if item.get("role") == "tool")
    assert "artifact" not in tool_entry
    assert "alpha" in tool_entry["content"]
    run_dir = agent.run_store.run_dir(agent.current_task_state.run_id)
    assert not (run_dir / "artifacts").exists()


def test_history_tool_result_tag_carries_the_artifact_pointer(tmp_path):
    _big_file(tmp_path)
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"big.txt","start":1,"end":900}}</tool>',
            "<final>done</final>",
        ],
    )
    agent.ask("read the big file")

    history_text = agent.context_manager.render_history_text()

    assert 'artifact="artifacts/' in history_text
    assert 'lines="' in history_text


def test_write_artifact_is_atomic_and_deduplicates(tmp_path):
    agent = build_agent(tmp_path, [])
    store = agent.run_store

    first, lines = store.write_artifact("run_x", 1, "run_shell", "a\nb\nc")
    second, _ = store.write_artifact("run_x", 2, "run_shell", "a\nb\nc")
    third, _ = store.write_artifact("run_x", 3, "run_shell", "different")

    assert first == second
    assert third != first
    assert lines == 3
    assert not list(store.artifacts_dir("run_x").glob("*.tmp"))
    assert len(list(store.artifacts_dir("run_x").glob("*.txt"))) == 2


def test_read_artifact_is_advertised_to_the_model(tmp_path):
    agent = build_agent(tmp_path, [])

    assert "read_artifact" in agent.tools
    assert not agent.tools["read_artifact"]["risky"]
    assert agent.tools["read_artifact"]["path_scope"] == "run_dir"
    assert "read_artifact" in agent.prefix
    definitions = json.dumps(
        toolkit.native_tool_definitions(agent.tools, "anthropic_messages")
    )
    assert "read_artifact" in definitions
