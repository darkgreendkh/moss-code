import json
import importlib
from dataclasses import replace
from unittest.mock import patch

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
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


def structured_request(tmp_path):
    workspace_marker = "WORKSPACE-INJECTION-DO-NOT-TRUST"
    tool_marker = "TOOL-INJECTION-DO-NOT-TRUST"
    (tmp_path / "README.md").write_text(workspace_marker + "\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )
    agent.record(
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "README.md"},
            "content": tool_marker,
            "created_at": "2026-08-05T00:00:00Z",
            "call_id": "call-1",
        }
    )
    bundle = agent.context_manager.build_bundle("inspect")
    return replace(bundle.request, cache_key=None), workspace_marker, tool_marker


def test_anthropic_payload_keeps_repository_and_tool_text_out_of_system(tmp_path):
    request, workspace_marker, tool_marker = structured_request(tmp_path)
    captured = {}
    client = AnthropicCompatibleModelClient(
        model="claude-opus-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    def fake_urlopen(http_request, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return FakeResponse({"content": [{"type": "text", "text": "<final>ok</final>"}], "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request)

    system_text = "\n".join(block["text"] for block in captured["body"]["system"])
    messages_text = json.dumps(captured["body"]["messages"], ensure_ascii=False)
    assert "You are moss" in system_text
    assert workspace_marker not in system_text
    assert tool_marker not in system_text
    assert workspace_marker in messages_text
    assert tool_marker in messages_text
    assert "<tool_result untrusted=\\\"true\\\"" in messages_text


def test_openai_payload_uses_developer_role_only_for_platform_blocks(tmp_path):
    request, workspace_marker, tool_marker = structured_request(tmp_path)
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
        return FakeResponse({"output_text": "<final>ok</final>", "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request)

    developer = captured["body"]["input"][0]
    developer_text = json.dumps(developer, ensure_ascii=False)
    remaining_text = json.dumps(captured["body"]["input"][1:], ensure_ascii=False)
    assert developer["role"] == "developer"
    assert "You are moss" in developer_text
    assert workspace_marker not in developer_text
    assert tool_marker not in developer_text
    assert workspace_marker in remaining_text
    assert tool_marker in remaining_text


def test_unknown_model_capabilities_fail_closed_without_url_guessing():
    capabilities = importlib.import_module("moss.providers.capabilities")

    unknown = capabilities.capabilities_for("openai", "private-model")
    known = capabilities.capabilities_for("openai", "gpt-5-5")

    assert unknown.supports_cache is False
    assert unknown.context_window == 32000
    assert known.supports_cache is True
    assert known.cache_style == "openai_prefix"


def test_usage_probe_can_detect_cache_support_after_a_conservative_default():
    capabilities = importlib.import_module("moss.providers.capabilities")

    class Client:
        native_tool_format = "anthropic_messages"

    client = Client()
    client.capabilities = capabilities.capabilities_for("unknown", "private-model")

    detected = capabilities.probe(
        client,
        {"cache_read_tokens": 12, "cache_metrics_available": True},
    )

    assert detected.supports_cache is True
    assert detected.reports_cache_usage is True
    assert detected.cache_style == "anthropic_breakpoint"


def test_anthropic_cache_breakpoints_cover_system_tools_and_penultimate_message(tmp_path):
    request, _, _ = structured_request(tmp_path)
    request = replace(
        request,
        cache_key="stable-key",
        tools=(
            {
                "name": "read_file",
                "description": "Read a file.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ),
    )
    captured = {}
    client = AnthropicCompatibleModelClient(
        model="claude-opus-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    def fake_urlopen(http_request, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return FakeResponse({"content": [{"type": "text", "text": "<final>ok</final>"}], "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request)

    body = captured["body"]
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_disabled_cache_request_contains_no_provider_cache_fields(tmp_path):
    request, _, _ = structured_request(tmp_path)
    captured = {}
    client = AnthropicCompatibleModelClient(
        model="claude-opus-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    def fake_urlopen(http_request, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return FakeResponse({"content": [{"type": "text", "text": "<final>ok</final>"}], "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request)

    assert "cache_control" not in json.dumps(captured["body"])


def test_openai_structured_request_disables_storage_and_sends_stable_cache_key(tmp_path):
    request, _, _ = structured_request(tmp_path)
    request = replace(request, cache_key="stable-key")
    captured = {}
    client = OpenAICompatibleModelClient(
        model="gpt-5-5",
        base_url="https://custom-gateway.example/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    def fake_urlopen(http_request, timeout):
        captured["body"] = json.loads(http_request.data.decode("utf-8"))
        return FakeResponse({"output_text": "<final>ok</final>", "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request)

    assert captured["body"]["store"] is False
    assert captured["body"]["prompt_cache_key"] == "stable-key"
    assert client.supports_prompt_cache is True
