import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext


def build_agent(tmp_path, client=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=client or FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )


def write_skill(root, name):
    skills_dir = root / ".moss" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\nbody\n",
        encoding="utf-8",
    )


def test_run_freezes_skill_registry_and_cache_key_until_next_run(tmp_path):
    agent = build_agent(tmp_path)
    agent.begin_run()
    before_key = agent.prefix_state.stable_hash

    write_skill(agent.root, "late")
    refresh = agent.refresh_prefix()

    assert refresh["registry_drift"] is True
    assert agent.prefix_state.stable_hash == before_key
    assert "late" not in agent.skills

    agent.end_run()
    agent.begin_run()

    assert "late" in agent.skills
    assert agent.prefix_state.stable_hash != before_key


class SkillAddingClient(FakeModelClient):
    def __init__(self, root):
        super().__init__(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
                "<final>done</final>",
            ]
        )
        self.root = root
        self.keys = []

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.keys.append(kwargs.get("prompt_cache_key"))
        if len(self.keys) == 1:
            write_skill(self.root, "during_run")
        return super().complete(prompt, max_new_tokens, **kwargs)


def test_agent_run_emits_registry_drift_and_keeps_cache_key_stable(tmp_path):
    client = SkillAddingClient(tmp_path)
    client.supports_prompt_cache = True
    agent = build_agent(tmp_path, client=client)

    assert agent.ask("inspect") == "done"
    assert len(set(client.keys)) == 1

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state.run_id)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    drift = [event for event in events if event["event"] == "tool_registry_drift"]
    assert len(drift) == 1
    assert drift[0]["added"] == ["during_run"]


def test_reload_registry_applies_disk_changes_outside_a_run(tmp_path):
    agent = build_agent(tmp_path)
    before_key = agent.prefix_state.stable_hash
    write_skill(agent.root, "manual")

    result = agent.reload_registry()

    assert result["added"] == ["manual"]
    assert "manual" in agent.skills
    assert agent.prefix_state.stable_hash != before_key
