from moss.prompt_prefix import build_prompt_prefix, tool_signature
from moss.tools import build_tool_registry
from moss.workspace import WorkspaceContext


def _workspace(status, **overrides):
    fields = dict(
        cwd="/repo",
        repo_root="/repo",
        branch="main",
        default_branch="main",
        status=status,
        recent_commits=["abc init"],
        project_docs={},
    )
    fields.update(overrides)
    return WorkspaceContext(**fields)


class _Agent:
    depth = 0
    max_depth = 1

    def __init__(self, root):
        self.root = root


def test_tool_signature_is_stable_across_registry_insertion_order(tmp_path):
    tools = {
        "b": {"schema": {"path": "str"}, "risky": False, "description": "B", "run": object()},
        "a": {"schema": {"command": "str"}, "risky": True, "description": "A", "run": object()},
    }
    reordered = {"a": tools["a"], "b": tools["b"]}

    assert tool_signature(tools) == tool_signature(reordered)


def test_build_prompt_prefix_renders_tools_and_workspace_metadata(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(workspace=workspace, tools=tools, built_at="2026-06-02T00:00:00+08:00")

    assert "You are moss" in prefix.text
    assert "Tools:" in prefix.text
    assert "- read_file(" in prefix.text
    assert "Workspace:" in prefix.text
    assert "Skills:" not in prefix.text
    assert prefix.hash
    assert prefix.workspace_fingerprint == workspace.fingerprint()
    assert prefix.tool_signature == tool_signature(tools)
    assert prefix.built_at == "2026-06-02T00:00:00+08:00"


def test_build_prompt_prefix_lists_skills_under_tools(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))
    skills = {"explain": {"name": "explain", "description": "Use when explaining.", "body": "x", "path": "p"}}

    prefix = build_prompt_prefix(workspace=workspace, tools=tools, skills=skills)

    assert "Skills:" in prefix.text
    assert "- explain: Use when explaining." in prefix.text
    assert prefix.text.index("Tools:") < prefix.text.index("Skills:") < prefix.text.index("Valid response examples:")
    assert prefix.skill_signature


def test_stable_hash_is_invariant_to_workspace_status_changes(tmp_path):
    tools = build_tool_registry(_Agent(tmp_path))

    clean = build_prompt_prefix(workspace=_workspace("clean"), tools=tools)
    dirty = build_prompt_prefix(workspace=_workspace(" M moss/runtime.py"), tools=tools)

    # workspace 段（git status）变了 -> 整段 prefix 文本与 hash 必然变，
    # 这样 prompt 仍会反映最新仓库状态。
    assert clean.text != dirty.text
    assert clean.hash != dirty.hash
    # 但「稳定头」没变 -> stable_hash 不变 -> prompt_cache_key 不会随 agent
    # 自己的文件改动每轮抖动。这是 change 2 的核心保证。
    assert clean.stable_hash
    assert clean.stable_hash == dirty.stable_hash
    assert clean.stable_hash != clean.hash
