import json
from unittest.mock import patch

from moss import Moss, SessionStore, WorkspaceContext
from moss.context.model_request import Block, Message, ModelRequest
from moss.output_parser import parse_model_actions
from moss.providers.clients import (
    AnthropicCompatibleModelClient,
    OpenAICompatibleModelClient,
    _anthropic_structured_messages,
    _extract_anthropic_text,
    _extract_openai_text,
)


class FakeResponse:
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class FakeSseResponse(FakeResponse):
    headers = {"Content-Type": "text/event-stream"}

    def read(self):
        return self.body.encode("utf-8")


def test_native_parser_preserves_all_ordered_tool_calls_and_call_ids():
    actions = parse_model_actions(
        [
            {"type": "tool", "name": "read_file", "args": {"path": "a.py"}, "call_id": "call-1"},
            {"type": "tool", "name": "read_file", "args": {"path": "b.py"}, "call_id": "call-2"},
        ],
        protocol="native",
    )

    assert [(action.name, action.args, action.call_id) for action in actions] == [
        ("read_file", {"path": "a.py"}, "call-1"),
        ("read_file", {"path": "b.py"}, "call-2"),
    ]


def test_native_prefix_omits_text_tool_protocol_examples(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    client = OpenAICompatibleModelClient(
        model="gpt-5-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    agent = Moss(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        tool_protocol="native",
    )

    assert agent.resolved_tool_protocol() == "native"
    assert '<tool>{"name"' not in agent.prefix_state.stable_text
    assert "Valid response examples:" not in agent.prefix_state.stable_text
    assert agent.context_manager.build_bundle("inspect").request.protocol == "native"


def test_forced_text_protocol_keeps_examples_on_native_capable_provider(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    client = OpenAICompatibleModelClient(
        model="gpt-5-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    agent = Moss(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        tool_protocol="text",
    )

    assert agent.resolved_tool_protocol() == "text"
    assert '<tool>{"name"' in agent.prefix_state.stable_text
    assert agent.context_manager.build_bundle("inspect").request.tools == ()


def test_anthropic_native_response_returns_multiple_calls_without_xml_conversion():
    client = AnthropicCompatibleModelClient(
        model="claude-opus-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    request = ModelRequest(
        system=(Block("rules", "rules"),),
        messages=(Message("user", (Block("inspect", "request", trust="user"),)),),
        protocol="native",
    )
    response = FakeResponse(
        {
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "toolu-1", "name": "read_file", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "toolu-2", "name": "read_file", "input": {"path": "b.py"}},
            ],
            "usage": {},
        }
    )

    with patch("urllib.request.urlopen", return_value=response):
        raw = client.complete_request(request)

    assert raw == [
        {"type": "tool", "name": "read_file", "args": {"path": "a.py"}, "call_id": "toolu-1"},
        {"type": "tool", "name": "read_file", "args": {"path": "b.py"}, "call_id": "toolu-2"},
    ]


def test_anthropic_text_extraction_never_converts_native_tool_use_to_xml():
    raw = _extract_anthropic_text(
        {
            "content": [
                {"type": "text", "text": "plain text response"},
                {"type": "tool_use", "id": "toolu-1", "name": "read_file", "input": {}},
            ]
        }
    )

    assert raw == "plain text response"


def test_openai_text_extraction_never_converts_native_tool_calls_to_xml():
    raw = _extract_openai_text(
        {
            "output_text": "plain text response",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": "{}",
                }
            ],
        }
    )

    assert raw == "plain text response"


def test_openai_native_sse_response_returns_tool_call_without_xml_conversion():
    client = OpenAICompatibleModelClient(
        model="gpt-5-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    request = ModelRequest(
        system=(Block("rules", "rules"),),
        messages=(Message("user", (Block("inspect", "request", trust="user"),)),),
        protocol="native",
    )
    event = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": '{"path":"a.py"}',
                }
            ],
            "usage": {},
        },
    }
    response = FakeSseResponse("data: " + json.dumps(event) + "\n\ndata: [DONE]\n")

    with patch("urllib.request.urlopen", return_value=response):
        raw = client.complete_request(request)

    assert raw == [
        {"type": "tool", "name": "read_file", "args": {"path": "a.py"}, "call_id": "call-1"}
    ]


def test_openai_native_sse_text_response_is_normalized_as_final_action():
    client = OpenAICompatibleModelClient(
        model="gpt-5-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )
    request = ModelRequest(
        messages=(Message("user", (Block("inspect", "request", trust="user"),)),),
        protocol="native",
    )
    response = FakeSseResponse(
        'data: {"type":"response.output_text.done","text":"done"}\n\ndata: [DONE]\n'
    )

    with patch("urllib.request.urlopen", return_value=response):
        raw = client.complete_request(request)

    assert raw == [{"type": "final", "text": "done"}]


def test_provider_payloads_return_native_tool_results_with_original_call_id():
    request = ModelRequest(
        system=(Block("rules", "rules"),),
        messages=(
            Message(
                "assistant",
                (Block('{"path":"a.py"}', "tool_call", source="read_file", trust="model"),),
                call_id="call-1",
            ),
            Message(
                "tool",
                (Block("contents", "tool_result", source="read_file", trust="tool"),),
                call_id="call-1",
            ),
            Message("user", (Block("continue", "request", trust="user"),)),
        ),
        protocol="native",
    )
    captured = {}
    client = OpenAICompatibleModelClient(
        model="gpt-5-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    def fake_urlopen(http_request, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return FakeResponse({"output_text": "done", "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request)

    assert captured["body"]["input"][1] == {
        "type": "function_call",
        "call_id": "call-1",
        "name": "read_file",
        "arguments": '{"path":"a.py"}',
    }
    assert captured["body"]["input"][2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "contents",
    }


def native_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=AnthropicCompatibleModelClient(
            model="claude-opus-5",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
            temperature=0.2,
            timeout=30,
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        tool_protocol="native",
    )


def record_native_call(agent, call_id, path):
    agent.record(
        {
            "role": "assistant",
            "name": "read_file",
            "args": {"path": path},
            "call_id": call_id,
            "native_tool_call": True,
            "content": "",
            "created_at": "2026-08-05T00:00:00Z",
        }
    )


def record_native_result(agent, call_id, path, content):
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": path},
            "content": content,
            "call_id": call_id,
            "created_at": "2026-08-05T00:00:01Z",
        }
    )


def assert_tool_use_is_balanced(payload_messages):
    """Anthropic /messages 的硬校验：每条 tool_use 的结果必须在紧随其后的那条消息里。"""
    for index, message in enumerate(payload_messages):
        pending = [
            item["id"] for item in message["content"] if item.get("type") == "tool_use"
        ]
        if not pending:
            continue
        following = payload_messages[index + 1] if index + 1 < len(payload_messages) else None
        assert following is not None, f"messages.{index}: tool_use 后面没有消息"
        answered = [
            item["tool_use_id"]
            for item in following["content"]
            if item.get("type") == "tool_result"
        ]
        assert answered == pending, f"messages.{index}: tool_use/tool_result 没配平"
    for index, message in enumerate(payload_messages):
        for item in message["content"]:
            if item.get("type") != "tool_result":
                continue
            previous = payload_messages[index - 1] if index else None
            assert previous is not None and any(
                block.get("type") == "tool_use" and block.get("id") == item["tool_use_id"]
                for block in previous["content"]
            ), f"messages.{index}: tool_result 没有对应的 tool_use"


def test_batched_native_calls_and_results_stay_in_one_message_each(tmp_path):
    agent = native_agent(tmp_path)
    # agent_loop 先把一轮里的两条调用都 record 下来，再逐条 record 结果——
    # 逐条翻译会得到 assistant/assistant/user/user，provider 直接 400。
    record_native_call(agent, "call-a", "CLAUDE.md")
    record_native_call(agent, "call-b", "README.md")
    record_native_result(agent, "call-a", "CLAUDE.md", "claude body")
    record_native_result(agent, "call-b", "README.md", "readme body")

    request = agent.context_manager.build_bundle("介绍一下你能干嘛").request
    messages = _anthropic_structured_messages(request)

    assert_tool_use_is_balanced(messages)
    calls = [message for message in messages if message["content"][0].get("type") == "tool_use"]
    assert len(calls) == 1
    assert [item["id"] for item in calls[0]["content"]] == ["call-a", "call-b"]


def test_native_call_without_a_recorded_result_is_balanced_not_dropped(tmp_path):
    agent = native_agent(tmp_path)
    record_native_call(agent, "call-a", "CLAUDE.md")
    record_native_call(agent, "call-b", "README.md")
    record_native_result(agent, "call-a", "CLAUDE.md", "claude body")

    messages = _anthropic_structured_messages(
        agent.context_manager.build_bundle("continue").request
    )

    assert_tool_use_is_balanced(messages)
    results = next(
        message for message in messages if message["content"][0].get("type") == "tool_result"
    )
    assert results["content"][0]["content"] == "claude body"
    assert "no result recorded" in results["content"][1]["content"]


def test_orphan_native_result_is_downgraded_to_text(tmp_path):
    agent = native_agent(tmp_path)
    # 调用那半截被裁掉/回退掉了：没有 tool_use 的 tool_result 一样会被拒收。
    record_native_result(agent, "call-a", "CLAUDE.md", "claude body")

    messages = _anthropic_structured_messages(
        agent.context_manager.build_bundle("continue").request
    )

    assert_tool_use_is_balanced(messages)
    assert "tool_result" not in {
        item.get("type") for message in messages for item in message["content"]
    }
    assert any(
        "claude body" in item.get("text", "")
        for message in messages
        for item in message["content"]
    )


def test_interleaved_runtime_notice_does_not_split_a_native_tool_turn(tmp_path):
    agent = native_agent(tmp_path)
    record_native_call(agent, "call-a", "CLAUDE.md")
    record_native_call(agent, "call-b", "README.md")
    record_native_result(agent, "call-a", "CLAUDE.md", "claude body")
    # instruction notice 会挤在两条工具结果中间。
    agent.record({"role": "system", "content": "Runtime notice: x", "created_at": "2026-08-05T00:00:02Z"})
    record_native_result(agent, "call-b", "README.md", "readme body")

    messages = _anthropic_structured_messages(
        agent.context_manager.build_bundle("continue").request
    )

    assert_tool_use_is_balanced(messages)
    assert any(
        "Runtime notice: x" in item.get("text", "")
        for message in messages
        for item in message["content"]
    )
