from pathlib import Path

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.extensions.skills import build_skill_registry, parse_skill_file
from moss.context.prefix import skill_signature


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


# --- spec-09 §9.4：渐进披露 / 能力覆盖 / scope / 供应链 -------------------


def _write_rich_skill(root, name, **frontmatter):
    skills_dir = Path(root) / ".moss" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    body = frontmatter.pop("body", "do the thing")
    lines = [f"name: {name}"] + [f"{key.replace('_', '-')}: {value}" for key, value in frontmatter.items()]
    (skills_dir / f"{name}.md").write_text(
        "---\n" + "\n".join(lines) + f"\n---\n\n{body}\n", encoding="utf-8"
    )


def test_frontmatter_parses_list_fields(tmp_path):
    _write_rich_skill(
        tmp_path,
        "bench",
        description="run benchmarks",
        allowed_tools="[run_shell, read_file]",
        scope='["benchmarks/**"]',
        resources="[scripts/bench.sh]",
        source="https://example.com/bench.md",
    )

    skill = build_skill_registry(tmp_path)["bench"]

    assert skill["allowed_tools"] == ("run_shell", "read_file")
    assert skill["scope"] == ("benchmarks/**",)
    assert skill["resources"] == ("scripts/bench.sh",)
    assert skill["source"] == "https://example.com/bench.md"
    assert len(skill["sha256"]) == 64


def test_twenty_skills_cost_under_four_hundred_prefix_tokens(tmp_path):
    """spec-09 §9.4 验收：稳定前缀不能随 skill 数量线性膨胀。"""
    from moss.context.token_budget import estimate_tokens

    for index in range(20):
        _write_rich_skill(
            tmp_path,
            f"skill-{index:02d}",
            description="A fairly long description that would otherwise eat the whole prefix. " * 4,
        )
    agent = build_agent(tmp_path)

    section = agent.prefix.split("Skills:\n", 1)[1].split("\n\n", 1)[0]

    assert len(section.splitlines()) == 20, "每个 skill 都必须还在列表里，不能被整段截掉"
    assert estimate_tokens(section) < 400


def test_use_skill_lists_resources_without_injecting_them(tmp_path):
    _write_rich_skill(tmp_path, "bench", description="d", resources="[scripts/bench.sh]", body="step one")
    agent = build_agent(tmp_path)

    result = agent.run_tool("use_skill", {"name": "bench"})

    assert "step one" in result
    assert "scripts/bench.sh" in result
    assert "read_file" in result


def test_allowed_tools_tightens_the_run_allowlist(tmp_path):
    _write_rich_skill(tmp_path, "reader", description="d", allowed_tools="[read_file, use_skill]")
    agent = build_agent(tmp_path)
    assert agent.effective_allowed_tools() is None

    agent.run_tool("use_skill", {"name": "reader"})

    assert set(agent.effective_allowed_tools()) == {"read_file", "use_skill"}
    assert "not allowed in this run" in agent.run_tool("list_files", {"path": "."})


def test_allowed_tools_cannot_escalate_beyond_the_run_allowlist(tmp_path):
    """越权声明 fail-closed 拒绝，而不是静默取交集降级运行。"""
    _write_rich_skill(tmp_path, "greedy", description="d", allowed_tools="[run_shell]")
    agent = build_agent(tmp_path, allowed_tools=("read_file", "use_skill"))

    result = agent.run_tool("use_skill", {"name": "greedy"})

    assert "outside this run's allowlist" in result
    assert agent.active_skill is None


def test_allowed_tools_rejects_unknown_tool_names(tmp_path):
    _write_rich_skill(tmp_path, "typo", description="d", allowed_tools="[reed_file]")
    agent = build_agent(tmp_path)

    assert "unknown tools" in agent.run_tool("use_skill", {"name": "typo"})


def test_skill_capability_override_expires_with_the_run(tmp_path):
    _write_rich_skill(tmp_path, "reader", description="d", allowed_tools="[read_file, use_skill]")
    agent = build_agent(
        tmp_path, outputs=['<tool>{"name":"use_skill","args":{"name":"reader"}}</tool>', "<final>ok</final>"]
    )

    agent.ask("use the skill")

    # 覆盖是临时的：落盘的"永久放开"会变成一个没人记得的后门。
    assert agent.active_skill is None
    assert agent.effective_allowed_tools() is None


def test_scope_hit_adds_one_hint_line_not_the_body(tmp_path):
    _write_rich_skill(tmp_path, "bench", description="d", scope='["benchmarks/**"]', body="SECRET-BODY")
    agent = build_agent(tmp_path)
    agent.memory.remember_file("benchmarks/run.py")

    hint = agent.skill_scope_hint()

    assert "bench" in hint
    assert "SECRET-BODY" not in hint


def test_scope_miss_produces_no_hint(tmp_path):
    _write_rich_skill(tmp_path, "bench", description="d", scope='["benchmarks/**"]')
    agent = build_agent(tmp_path)
    agent.memory.remember_file("src/main.py")

    assert agent.skill_scope_hint() == ""


def test_changed_third_party_skill_needs_confirmation(tmp_path):
    _write_rich_skill(tmp_path, "vendor", description="d", source="https://example.com/v.md", body="v1")
    agent = build_agent(tmp_path, approval_policy="auto")
    agent.run_tool("use_skill", {"name": "vendor"})

    _write_rich_skill(tmp_path, "vendor", description="d", source="https://example.com/v.md", body="v2 EVIL")
    agent.reload_registry()
    agent.approval_policy = "never"

    result = agent.run_tool("use_skill", {"name": "vendor"})

    assert "not confirmed" in result
    assert "EVIL" not in result


def test_trusted_third_party_skill_loads_without_reconfirmation(tmp_path):
    _write_rich_skill(tmp_path, "vendor", description="d", source="https://example.com/v.md", body="v1")
    agent = build_agent(tmp_path, approval_policy="auto")
    agent.run_tool("use_skill", {"name": "vendor"})

    agent.approval_policy = "never"

    assert "v1" in agent.run_tool("use_skill", {"name": "vendor"})


def test_local_skills_do_not_trigger_supply_chain_prompts(tmp_path):
    """本地手写 skill 每改一行都确认一次，只会训练用户闭眼按 y。"""
    _write_rich_skill(tmp_path, "mine", description="d", body="v1")
    agent = build_agent(tmp_path, approval_policy="never")

    assert "v1" in agent.run_tool("use_skill", {"name": "mine"})


def test_skill_signature_catches_a_body_only_change(tmp_path):
    _write_rich_skill(tmp_path, "mine", description="same", body="v1")
    before = skill_signature(build_skill_registry(tmp_path))

    _write_rich_skill(tmp_path, "mine", description="same", body="v2")

    assert skill_signature(build_skill_registry(tmp_path)) != before
