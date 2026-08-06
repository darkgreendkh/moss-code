"""能力标签与路径作用域（spec-03 §4.2）。"""

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.execution.safety.policy import CAPABILITIES, Policy, parse_capability_rules
from moss.execution.registry import BASE_TOOL_SPECS, DELEGATE_TOOL_SPEC, USE_SKILL_TOOL_SPEC, ToolSpec


def _spec(**overrides):
    fields = dict(
        name="demo",
        fields={},
        risky=True,
        description="demo",
        capabilities=frozenset({"fs_write"}),
    )
    fields.update(overrides)
    return ToolSpec(**fields)


def test_every_registered_risky_tool_declares_capabilities():
    """fail-closed 的意义就在这：忘了声明会立刻炸在测试里。"""
    specs = list(BASE_TOOL_SPECS.values()) + [DELEGATE_TOOL_SPEC, USE_SKILL_TOOL_SPEC]

    for spec in specs:
        assert spec.capabilities <= CAPABILITIES, spec.name
        if spec.risky:
            assert spec.capabilities, f"{spec.name} is risky but declares no capabilities"


def test_a_risky_tool_without_capabilities_is_refused():
    decision = Policy.build().decide(_spec(capabilities=frozenset()), {})

    assert decision.allowed is False
    # 报错要直接告诉作者该声明什么，否则只能靠猜。
    assert "add capabilities" in decision.reason


def test_unknown_capabilities_are_refused():
    decision = Policy.build().decide(_spec(capabilities=frozenset({"telepathy"})), {})

    assert decision.allowed is False
    assert "telepathy" in decision.reason


def test_protected_paths_are_denied_by_default():
    policy = Policy.build()

    for path in (".git/config", ".github/workflows/ci.yml", ".env", ".moss/sessions/a.json"):
        decision = policy.decide(_spec(), {"path": path}, resolved_paths=(path,))
        assert decision.allowed is False, path
        assert "denied by policy" in decision.reason


def test_a_protected_directory_itself_is_denied():
    decision = Policy.build().decide(_spec(), {"path": ".git"}, resolved_paths=(".git",))

    assert decision.allowed is False


def test_ordinary_source_paths_are_allowed():
    decision = Policy.build().decide(_spec(), {"path": "src/mod.py"}, resolved_paths=("src/mod.py",))

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert any("fs_write" in effect for effect in decision.effects)


def test_allowlist_restricts_the_scope():
    policy = Policy.build(allow={"fs_write": ("src/**",)})

    assert policy.decide(_spec(), {}, resolved_paths=("src/a.py",)).allowed is True
    outside = policy.decide(_spec(), {}, resolved_paths=("docs/a.md",))
    assert outside.allowed is False
    assert "outside the allowed scope" in outside.reason


def test_extra_deny_rules_are_merged_with_the_defaults():
    policy = Policy.build(deny={"fs_write": ("migrations/**",)})

    assert policy.decide(_spec(), {}, resolved_paths=("migrations/001.sql",)).allowed is False
    # 追加不能把默认清单挤掉。
    assert policy.decide(_spec(), {}, resolved_paths=(".env",)).allowed is False


def test_read_only_policy_refuses_anything_beyond_reading():
    policy = Policy.build(read_only=True)

    assert policy.decide(_spec(capabilities=frozenset({"fs_read"})), {}).allowed is True
    refused = policy.decide(_spec(), {}, resolved_paths=("src/a.py",))
    assert refused.allowed is False
    assert "read-only" in refused.reason


def test_read_only_tools_need_no_approval():
    decision = Policy.build().decide(
        _spec(risky=False, capabilities=frozenset({"fs_read"})), {}, resolved_paths=("src/a.py",)
    )

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_parse_capability_rules_handles_both_forms():
    assert parse_capability_rules(["network"]) == {"network": ("**",)}
    assert parse_capability_rules(["fs_write=src/**,tests/**"]) == {"fs_write": ("src/**", "tests/**")}
    assert parse_capability_rules(["fs_write=a/**", "fs_write=b/**"]) == {"fs_write": ("a/**", "b/**")}
    assert parse_capability_rules([]) == {}


def _build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        **kwargs,
    )


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".env", ".git/config"])
def test_writes_to_protected_paths_are_blocked_end_to_end(tmp_path, path):
    """改掉 CI 配置等于把项目所有校验一起关掉，这不该和改 src/x.py 同档。"""
    agent = _build_agent(tmp_path)

    result = agent.run_tool("write_file", {"path": path, "content": "x"})

    assert "policy refused" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "capability_denied"
    assert not (tmp_path / path).exists()


def test_ordinary_writes_still_work(tmp_path):
    agent = _build_agent(tmp_path)

    result = agent.run_tool("write_file", {"path": "src/mod.py", "content": "x = 1\n"})

    assert result.startswith("wrote")
    assert (tmp_path / "src" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"


def test_cli_deny_rules_reach_the_runtime(tmp_path):
    agent = _build_agent(tmp_path, policy=Policy.build(deny={"fs_write": ("docs/**",)}))

    assert "policy refused" in agent.run_tool("write_file", {"path": "docs/a.md", "content": "x"})
    assert agent.run_tool("write_file", {"path": "src/a.py", "content": "x"}).startswith("wrote")
