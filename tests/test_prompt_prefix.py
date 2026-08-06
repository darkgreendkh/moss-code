import hashlib

import moss.context.prefix as prompt_module
from moss.context.prefix import build_prompt_prefix, tool_signature
from moss.tools import ToolField, build_tool_registry
from moss.context.repository.workspace import WorkspaceContext


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


def test_stable_prefix_includes_all_memory_tools(tmp_path):
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(WorkspaceContext.build(tmp_path), tools)

    for name in ("memory_write", "memory_update", "memory_delete", "memory_search"):
        assert name in prefix.text


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
    assert prefix.prompt_version == prompt_module.PROMPT_VERSION == "p1"


def test_system_prompt_file_overrides_builtin_head_and_versions_by_content(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    prompt_path = tmp_path / ".moss" / "prompts" / "system.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("CUSTOM SYSTEM\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(workspace=workspace, tools=tools)

    assert prefix.stable_text == "CUSTOM SYSTEM"
    expected_hash = hashlib.sha256("CUSTOM SYSTEM\n".encode("utf-8")).hexdigest()[:12]
    assert prefix.prompt_version == f"file:{expected_hash}"
    assert "You are moss" not in prefix.stable_text


def test_prompt_prefix_renders_executable_schema_fields_as_concise_text(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(workspace=workspace, tools=tools)

    assert isinstance(tools["read_file"]["schema"]["start"], ToolField)
    assert "- read_file(path: str, start: int=1, end: int=800)" in prefix.text
    assert "- run_shell(command: str, timeout: int=60)" in prefix.text
    assert "ToolField(" not in prefix.text


def test_tool_signature_changes_when_executable_schema_field_changes():
    base = {
        "run_shell": {
            "schema": {"command": ToolField("str"), "timeout": ToolField("int", required=False, default=60, minimum=1, maximum=600)},
            "risky": True,
            "description": "Run shell",
        }
    }
    changed = {
        "run_shell": {
            "schema": {"command": ToolField("str"), "timeout": ToolField("int", required=False, default=30, minimum=1, maximum=600)},
            "risky": True,
            "description": "Run shell",
        }
    }

    assert tool_signature(base) != tool_signature(changed)


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
