"""段落顺序、constraints 段与上下文健康度（spec-06 §4.5）。

顺序不是审美问题：硬约束离当前请求越远越容易被忽略，而"历史"和"约束"
混在一段里时，模型最常见的失败就是把历史里的旧要求当成当前指令。
"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.context.manager import (
    CONSTRAINTS_SECTION,
    DEFAULT_REDUCTION_ORDER,
    SECTION_ORDER,
    SECTION_PURPOSE,
    ContextManager,
)


def build_agent(tmp_path, outputs=(), **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def test_section_order_puts_constraints_after_history_and_before_the_request():
    assert SECTION_ORDER == (
        "prefix",
        "history",
        "memory",
        "relevant_memory",
        "constraints",
        "current_request",
    )
    assert SECTION_ORDER.index(CONSTRAINTS_SECTION) > SECTION_ORDER.index("history")
    assert SECTION_ORDER.index(CONSTRAINTS_SECTION) < SECTION_ORDER.index("current_request")
    # 硬约束最后才削：削掉之后模型看起来还在工作，但它已经不知道该守什么了。
    assert DEFAULT_REDUCTION_ORDER[-1] == CONSTRAINTS_SECTION


def test_every_section_carries_an_explicit_purpose_line(tmp_path):
    agent = build_agent(tmp_path)
    agent.set_plan([{"id": "1", "title": "read the parser", "status": "in_progress"}])

    prompt, metadata = ContextManager(agent).build("what next?")

    for section, purpose in SECTION_PURPOSE.items():
        if metadata["sections"][section]["rendered_chars"]:
            assert purpose in prompt, section
    assert prompt.index(SECTION_PURPOSE["history"]) < prompt.index(SECTION_PURPOSE[CONSTRAINTS_SECTION])
    assert prompt.index(SECTION_PURPOSE[CONSTRAINTS_SECTION]) < prompt.index(
        SECTION_PURPOSE["current_request"]
    )


def test_constraints_section_carries_the_plan_instead_of_the_prefix_tail(tmp_path):
    agent = build_agent(tmp_path)
    agent.set_plan([{"id": "1", "title": "read the parser", "status": "in_progress"}])

    prompt, metadata = ContextManager(agent).build("keep going")

    assert "read the parser" in prompt
    # 计划从稳定前缀尾部搬走了：留在那里会让 prompt 缓存每轮失效。
    assert "read the parser" not in agent.prefix
    assert prompt.index("Transcript:") < prompt.index("read the parser")
    assert metadata["sections"][CONSTRAINTS_SECTION]["rendered_chars"] > 0


def test_constraints_section_lists_recent_failures(tmp_path):
    agent = build_agent(tmp_path)
    agent.record_tool_outcome("read_file", {"path": "missing.py"}, {"tool_error_code": "invalid_arguments"})
    agent.record_tool_outcome("read_file", {"path": "fine.py"}, {"tool_error_code": ""})

    prompt, _ = ContextManager(agent).build("try again")

    assert "Recent failures" in prompt
    assert "read_file missing.py -> invalid_arguments" in prompt
    assert "fine.py" not in prompt.split("Recent failures", 1)[1]


def test_empty_constraints_section_is_omitted_entirely(tmp_path):
    agent = build_agent(tmp_path)

    prompt, metadata = ContextManager(agent).build("hello")

    assert metadata["sections"][CONSTRAINTS_SECTION]["rendered_chars"] == 0
    assert SECTION_PURPOSE[CONSTRAINTS_SECTION] not in prompt


def test_constraints_reach_the_structured_request_as_a_platform_block(tmp_path):
    agent = build_agent(tmp_path)
    agent.set_plan([{"id": "1", "title": "read the parser", "status": "in_progress"}])

    bundle = agent.context_manager.build_bundle("keep going")
    blocks = [block for message in bundle.request.messages for block in message.blocks]
    constraints = [block for block in blocks if block.kind == "constraints"]

    assert len(constraints) == 1
    assert constraints[0].trust == "platform"
    assert "read the parser" in constraints[0].text
    # 结构化请求的块顺序必须和 prompt 文本一致，否则两者不是同一份东西。
    kinds = [block.kind for block in blocks]
    assert kinds.index("history") < kinds.index("constraints") < kinds.index("request")


def test_context_health_metrics_are_reported_every_turn(tmp_path):
    agent = build_agent(tmp_path)
    agent.record({"role": "user", "content": "unrelated chatter about gardening", "created_at": "2026-04-07T09:00:00+00:00"})
    agent.record({"role": "assistant", "content": "parser tokens live in output_parser", "created_at": "2026-04-07T09:01:00+00:00"})

    _, metadata = ContextManager(agent).build("where is the parser?")
    health = metadata["context_health"]

    assert 0.0 < health["context_utilization"] < 1.0
    assert health["context_window"] == agent.model_client.capabilities.context_window
    assert set(health["section_share"]) == set(SECTION_ORDER)
    assert abs(sum(health["section_share"].values()) - 1.0) < 0.35
    assert 0.0 <= health["distractor_ratio"] <= 1.0
    assert health["history_staleness"] == 2


def test_distractor_ratio_rises_when_history_has_nothing_to_do_with_the_request(tmp_path):
    focused = build_agent(tmp_path / "focused")
    noisy = build_agent(tmp_path / "noisy")
    for index in range(6):
        focused.record(
            {
                "role": "user",
                "content": f"the parser tokenizes parser input {index}",
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )
        noisy.record(
            {
                "role": "user",
                "content": f"unrelated gardening notes about tulips {index}",
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )

    _, focused_metadata = ContextManager(focused).build("fix the parser")
    _, noisy_metadata = ContextManager(noisy).build("fix the parser")

    assert (
        focused_metadata["context_health"]["distractor_ratio"]
        < noisy_metadata["context_health"]["distractor_ratio"]
    )


def test_health_metrics_reach_the_trace(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])

    agent.ask("hello")

    prompt_built = next(
        event
        for event in agent.run_store.read_trace(agent.current_task_state.run_id)
        if event["event"] == "prompt_built"
    )
    health = prompt_built["prompt_metadata"]["context_health"]
    assert "context_utilization" in health
    assert "section_share" in health
