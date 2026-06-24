from pathlib import Path

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.skills import build_skill_registry, parse_skill_file
from moss.prompt_prefix import skill_signature


def _write_skill(root, name, description, body):
    skills_dir = Path(root) / ".moss" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


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


def test_parse_skill_file_reads_frontmatter_and_body(tmp_path):
    path = tmp_path / "explain.md"
    path.write_text(
        "---\nname: explain-code\ndescription: Use when explaining code.\n---\n\nStep 1.\nStep 2.\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(path)

    assert skill["name"] == "explain-code"
    assert skill["description"] == "Use when explaining code."
    assert skill["body"] == "Step 1.\nStep 2."


def test_parse_skill_file_falls_back_to_stem_without_frontmatter(tmp_path):
    path = tmp_path / "free-form.md"
    path.write_text("just some instructions\n", encoding="utf-8")

    skill = parse_skill_file(path)

    assert skill["name"] == "free-form"
    assert skill["description"] == ""
    assert "just some instructions" in skill["body"]


def test_build_skill_registry_discovers_sorts_and_dedupes(tmp_path):
    _write_skill(tmp_path, "beta", "B skill", "b body")
    _write_skill(tmp_path, "alpha", "A skill", "a body")

    registry = build_skill_registry(tmp_path)

    assert list(registry.keys()) == ["alpha", "beta"]
    assert registry["alpha"]["description"] == "A skill"


def test_build_skill_registry_empty_when_dir_missing(tmp_path):
    assert build_skill_registry(tmp_path) == {}


def test_skill_signature_is_stable_across_insertion_order():
    a = {"name": "a", "description": "A"}
    b = {"name": "b", "description": "B"}
    assert skill_signature({"a": a, "b": b}) == skill_signature({"b": b, "a": a})


def test_use_skill_tool_is_absent_without_skills(tmp_path):
    agent = build_agent(tmp_path)
    assert "use_skill" not in agent.tools


def test_use_skill_tool_returns_body(tmp_path):
    _write_skill(tmp_path, "explain", "Use when explaining.", "Step 1. Read.\nStep 2. Summarize.")
    agent = build_agent(tmp_path)

    assert "use_skill" in agent.tools
    result = agent.run_tool("use_skill", {"name": "explain"})

    assert "Step 1. Read." in result


def test_use_skill_tool_rejects_unknown_skill(tmp_path):
    _write_skill(tmp_path, "explain", "Use when explaining.", "body")
    agent = build_agent(tmp_path)

    result = agent.run_tool("use_skill", {"name": "missing"})

    assert "unknown skill" in result


def test_refresh_prefix_rebuilds_when_skill_added_without_workspace_change(tmp_path):
    # 这是 change 1 与 change 2 的接合点：workspace 指纹没变，
    # 但新增了 skill，prefix 仍必须重建、prompt_cache_key 仍必须更新。
    agent = build_agent(tmp_path)
    before_key = agent.prefix_state.hash
    assert "use_skill" not in agent.tools

    _write_skill(agent.root, "explain", "Use when explaining.", "do x")
    refresh = agent.refresh_prefix()

    assert refresh["workspace_changed"] is False
    assert refresh["prefix_changed"] is True
    assert agent.prefix_state.hash != before_key
    assert "Skills:" in agent.prefix
    assert "- explain: Use when explaining." in agent.prefix
    assert "use_skill" in agent.tools
