from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.features.memory import LayeredMemory
from moss.features.memory_records import SourceRef, make_record
from moss.features.memory_store import MemoryStore, project_scope_key


def _record(root, text, *, scope="project", scope_key=None, topic="dependency-facts"):
    return make_record(
        scope=scope,
        scope_key=scope_key or project_scope_key(root),
        topic=topic,
        subject=text.split(" is ", 1)[0].lower(),
        text=text,
        tags=("scope",),
        trust="user",
        source_refs=(SourceRef(run_id="scope-test"),),
    )


def test_project_memory_does_not_leak_between_workspaces(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = LayeredMemory(workspace_root=first_root)
    second = LayeredMemory(workspace_root=second_root)
    first.write_durable(
        scope="project",
        topic="dependency-facts",
        text="private codename is juniper",
        trust="user",
        source_refs=(SourceRef(run_id="run-first"),),
    )

    assert first.search("juniper", limit=5)
    assert second.search("juniper", limit=5) == []


def test_session_memory_is_visible_only_to_matching_session(tmp_path):
    first = LayeredMemory(workspace_root=tmp_path, session_id="session-one")
    first.write_durable(
        scope="session",
        topic="key-decisions",
        text="temporary strategy is canary",
        trust="model",
        source_refs=(SourceRef(run_id="run-one"),),
    )

    resumed = LayeredMemory(workspace_root=tmp_path, session_id="session-one")
    other = LayeredMemory(workspace_root=tmp_path, session_id="session-two")

    assert resumed.search("canary", limit=5)
    assert other.search("canary", limit=5) == []


def test_global_memory_is_recalled_from_explicit_global_store(tmp_path):
    workspace = tmp_path / "repo"
    global_root = tmp_path / "home-memory"
    workspace.mkdir()
    global_store = MemoryStore(global_root)
    global_store.append(
        _record(workspace, "answer style is concise", scope="global", scope_key="global")
    )

    memory = LayeredMemory(
        workspace_root=workspace,
        session_id="session",
        global_memory_root=global_root,
    )

    match = memory.search("concise answer style", limit=5)[0]
    assert match["scope"] == "global"


def test_path_scope_is_boosted_for_recent_working_path(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    store = MemoryStore(tmp_path / ".moss" / "memory", workspace_root=tmp_path)
    store.append(_record(tmp_path, "formatter is black for source", scope="path", scope_key="src"))
    store.append(_record(tmp_path, "formatter is prettier for docs", scope="path", scope_key="docs"))
    memory = LayeredMemory(workspace_root=tmp_path, session_id="session")
    memory.remember_file("src/app.py")

    matches = memory.search("formatter", limit=5)

    assert [match["scope_key"] for match in matches[:2]] == ["src", "docs"]


def test_read_file_summary_carries_repo_map_symbols_and_exact_read_hint(tmp_path):
    target = tmp_path / "service.py"
    target.write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "def boot():\n"
        "    return Service().run()\n",
        encoding="utf-8",
    )
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )

    agent.run_tool("read_file", {"path": "service.py", "start": 1, "end": 6})

    summary = agent.memory.to_dict()["file_summaries"]["service.py"]
    assert summary["path"] == "service.py"
    assert summary["sha"]
    assert {symbol["name"] for symbol in summary["symbols"]} >= {"Service", "Service.run", "boot"}
    rendered = agent.memory.render_memory_text()
    assert "read_file(service.py, start=1, end=3)" in rendered
