import json
from unittest.mock import patch

from moss import Moss, SessionStore, WorkspaceContext
from moss.model_request import Block, Message, ModelRequest
from moss.output_parser import parse_model_actions
from moss.providers.clients import AnthropicCompatibleModelClient, OpenAICompatibleModelClient


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
