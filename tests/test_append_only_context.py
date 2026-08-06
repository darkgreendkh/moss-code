from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext


def build_agent(tmp_path, context_mode="rerender"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        context_mode=context_mode,
    )


def history_messages(bundle):
    return [
        message
        for message in bundle.request.messages
        if any(block.kind in {"history", "tool_result", "tool_call"} for block in message.blocks)
    ]


def test_rerender_remains_the_default_context_mode(tmp_path):
    agent = build_agent(tmp_path)

    bundle = agent.context_manager.build_bundle("hello")

    assert agent.context_mode == "rerender"
    assert bundle.metadata["context_mode"] == "rerender"
    assert any("\nTranscript:" in block.text for message in bundle.request.messages for block in message.blocks)


def test_append_only_keeps_existing_message_serialization_when_new_history_arrives(tmp_path):
    agent = build_agent(tmp_path, context_mode="append_only")
    agent.record({"role": "user", "content": "first", "created_at": "2026-08-05T00:00:00Z"})
    first_bundle = agent.context_manager.build_bundle("current")
    original = history_messages(first_bundle)

    agent.record({"role": "assistant", "content": "second", "created_at": "2026-08-05T00:01:00Z"})
    second_bundle = agent.context_manager.build_bundle("current")
    extended = history_messages(second_bundle)

    assert original == extended[: len(original)]
    assert len(extended) == len(original) + 1
    assert second_bundle.metadata["context_mode"] == "append_only"


def test_explicit_compaction_replaces_only_declared_message_range(tmp_path):
    agent = build_agent(tmp_path, context_mode="append_only")
    for index, content in enumerate(("zero", "one", "two")):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": content,
                "created_at": f"2026-08-05T00:0{index}:00Z",
            }
        )
    agent.session["compaction_artifacts"] = [
        {"start": 0, "end": 2, "summary": "summary of zero and one"}
    ]

    messages = history_messages(agent.context_manager.build_bundle("current"))

    assert len(messages) == 2
    assert messages[0].blocks[0].text == "summary of zero and one"
    assert messages[0].blocks[0].cache is True
    assert messages[1].blocks[0].text.endswith("two")


def test_cli_context_mode_reaches_runtime(tmp_path):
    from moss import build_agent, build_arg_parser

    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--provider", "ollama", "--context-mode", "append_only"]
    )

    assert build_agent(args).context_mode == "append_only"
