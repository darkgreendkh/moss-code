"""MCP 客户端与服务端（spec-09 §9.2）。

守四条：外部工具必须声明 capabilities（fail-closed）、注册期落白名单而不是
运行期动态发现、工具数膨胀时 prefix 不跟着线性膨胀、外部输出走同一套注入护栏。
"""

import io
import json

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.mcp.client import McpClient, McpError, flatten_content
from moss.mcp.registry import (
    McpServerConfig,
    build_mcp_tools,
    fields_from_json_schema,
    load_mcp_config,
    mcp_tool_name,
)
from moss.mcp.server import MossMcpServer
from moss.context.prefix import build_prompt_prefix, render_tool_lines
from moss.context.token_budget import estimate_tokens


class _FakeTransport:
    """把 JSON-RPC 消息在内存里对答，不起子进程。"""

    def __init__(self, tools, results=None, error=None):
        self.tools = tools
        self.results = results or {}
        self.error = error
        self.sent = []
        self.closed = False

    def roundtrip(self, payload, *, expect_reply=True):
        self.sent.append(payload)
        if not expect_reply:
            return None
        method = payload["method"]
        if self.error and method == "tools/call":
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"message": self.error}}
        result = {
            "initialize": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}},
            "tools/list": {"tools": self.tools},
        }.get(method)
        if result is None:
            name = payload["params"]["name"]
            text = self.results.get(name, f"ran {name}")
            result = {"content": [{"type": "text", "text": text}]}
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    def close(self):
        self.closed = True


def _client(tools=None, **kwargs):
    tools = tools if tools is not None else [
        {"name": "fetch", "description": "Fetch a URL.", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}
    ]
    client = McpClient(_FakeTransport(tools, **kwargs), name="demo")
    client.initialize()
    return client


def _write_config(root, payload):
    (root / ".moss").mkdir(parents=True, exist_ok=True)
    (root / ".moss" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _agent(tmp_path, outputs=(), **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


# --- 客户端 -------------------------------------------------------------


def test_initialize_then_list_tools():
    client = _client()

    assert [tool["name"] for tool in client.list_tools()] == ["fetch"]
    methods = [message["method"] for message in client.transport.sent]
    assert methods[:2] == ["initialize", "notifications/initialized"]


def test_call_tool_flattens_text_content():
    client = _client(results={"fetch": "hello body"})

    assert client.call_tool("fetch", {"url": "http://x"}) == "hello body"


def test_server_error_becomes_mcp_error():
    client = _client(error="upstream exploded")

    with pytest.raises(McpError, match="upstream exploded"):
        client.call_tool("fetch", {"url": "http://x"})


def test_non_text_content_is_summarized_not_inlined():
    """把 base64 图片塞进 prompt 既没用又能一口气吃掉整个预算。"""
    text = flatten_content(
        [{"type": "text", "text": "ok"}, {"type": "image", "data": "A" * 5000}]
    )

    assert "A" * 100 not in text
    assert "image content omitted" in text


# --- 配置与 fail-closed --------------------------------------------------


def test_server_config_requires_capabilities():
    with pytest.raises(ValueError, match="must declare capabilities"):
        McpServerConfig.parse("fs", {"command": ["cat"]})


def test_server_config_rejects_unknown_capabilities():
    with pytest.raises(ValueError, match="unknown capabilities"):
        McpServerConfig.parse("fs", {"command": ["cat"], "capabilities": ["telepathy"]})


def test_external_tools_always_carry_network_and_approval():
    """外部进程能连什么我们证明不了。证明不了就按最坏算。"""
    config = McpServerConfig.parse("fs", {"command": ["cat"], "capabilities": ["fs_read"]})

    assert config.capabilities == frozenset({"fs_read", "network"})


def test_bad_config_is_reported_not_fatal(tmp_path):
    _write_config(tmp_path, {"mcp": {"servers": {"broken": {"command": ["cat"]}}}})

    servers, errors, _ = load_mcp_config(tmp_path)

    assert servers == []
    assert errors and "must declare capabilities" in errors[0]


def test_unreachable_server_is_reported_not_fatal(tmp_path):
    _write_config(
        tmp_path, {"mcp": {"servers": {"gone": {"command": ["cat"], "capabilities": ["fs_read"]}}}}
    )
    problems = []

    def explode(server):
        raise OSError("no such binary")

    entries, clients, _ = build_mcp_tools(tmp_path, connect=explode, on_error=problems.append)

    assert entries == {} and clients == []
    assert problems and "unavailable" in problems[0]


# --- 注册期落白名单 ------------------------------------------------------


def test_mcp_tools_become_risky_specs_in_the_registry(tmp_path):
    _write_config(
        tmp_path, {"mcp": {"servers": {"demo": {"command": ["cat"], "capabilities": ["network"]}}}}
    )

    entries, _, _ = build_mcp_tools(tmp_path, connect=lambda server: _client())

    entry = entries[mcp_tool_name("demo", "fetch")]
    assert entry["risky"] is True
    assert entry["capabilities"] == frozenset({"network"})
    assert entry["description"].startswith("[mcp:demo]")


def test_only_listed_tools_are_registered(tmp_path):
    _write_config(
        tmp_path,
        {"mcp": {"servers": {"demo": {"command": ["cat"], "capabilities": ["network"], "tools": ["other"]}}}},
    )

    entries, _, _ = build_mcp_tools(tmp_path, connect=lambda server: _client())

    assert entries == {}


def test_mcp_tools_enter_the_agent_registry_and_signature(tmp_path):
    _write_config(
        tmp_path, {"mcp": {"servers": {"demo": {"command": ["cat"], "capabilities": ["network"]}}}}
    )
    baseline = _agent(tmp_path / "plain").tool_signature()

    def connect(server):
        return _client()

    from moss.mcp import registry as registrylib

    original = registrylib._connect
    registrylib._connect = connect
    try:
        agent = _agent(tmp_path)
    finally:
        registrylib._connect = original

    assert mcp_tool_name("demo", "fetch") in agent.tools
    # 进 tool_signature 才算真的进了白名单：注册表漂移要能被检出。
    assert agent.tool_signature() != baseline


def test_json_schema_becomes_tool_fields():
    fields = fields_from_json_schema(
        {"properties": {"url": {"type": "string"}, "depth": {"type": "integer", "default": 2}}, "required": ["url"]}
    )

    assert fields["url"].required is True and fields["url"].type == "str"
    assert fields["depth"].required is False and fields["depth"].default == 2


# --- 目录模式：工具数膨胀时 prefix 不跟着涨 ------------------------------


def _fake_tools(count):
    from moss.tools import ToolField

    return {
        f"tool_{index:02d}": {
            "schema": {
                "path": ToolField("str"),
                "start": ToolField("int", required=False, default=1),
                "end": ToolField("int", required=False, default=200),
            },
            "risky": index % 2 == 0,
            "description": "A tool with a description long enough to matter in the prefix budget.",
        }
        for index in range(count)
    }


def test_small_registries_keep_full_schemas():
    lines = render_tool_lines(_fake_tools(5))

    assert all("path: str" in line for line in lines)


def test_catalog_mode_drops_schemas_and_offers_describe_tool():
    lines = render_tool_lines(_fake_tools(30))

    assert not any("path: str" in line for line in lines)
    assert any("describe_tool" in line for line in lines)
    assert len([line for line in lines if line.startswith("- ")]) == 30


def test_thirty_tools_grow_the_prefix_by_under_twenty_percent(tmp_path):
    """spec-09 §9.2 验收：工具数 30 时 prefix token 增长 <20%。"""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    baseline = build_prompt_prefix(workspace, _fake_tools(14))
    grown = build_prompt_prefix(workspace, _fake_tools(30))

    before = estimate_tokens(baseline.stable_text)
    after = estimate_tokens(grown.stable_text)
    assert after < before * 1.2, f"{before} -> {after}"


def test_describe_tool_returns_the_full_schema(tmp_path):
    # describe_tool 只在目录模式下才注册——不然它是个没用处的工具。
    _write_config(tmp_path, {"mcp": {"catalog_threshold": 5}})
    agent = _agent(tmp_path)

    result = agent.run_tool("describe_tool", {"name": "read_file"})

    assert "read_file(" in result and "start: int=1" in result
    assert "capabilities: fs_read" in result


def test_describe_tool_rejects_unknown_names(tmp_path):
    _write_config(tmp_path, {"mcp": {"catalog_threshold": 5}})

    assert "unknown tool" in _agent(tmp_path).run_tool("describe_tool", {"name": "nope"})


def test_describe_tool_is_absent_below_the_catalog_threshold(tmp_path):
    assert "describe_tool" not in _agent(tmp_path).tools


# --- 服务端 -------------------------------------------------------------


def _server(tmp_path, **kwargs):
    return MossMcpServer(_agent(tmp_path), **kwargs)


def test_server_lists_only_read_only_tools(tmp_path):
    server = _server(tmp_path, exported_tools=("read_file", "write_file", "run_shell"))

    assert [tool["name"] for tool in server.tool_definitions()] == ["read_file"]


def test_server_calls_go_through_the_guarded_entry_point(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    server = _server(tmp_path)

    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "target.txt"}}}
    )

    assert "alpha" in reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is False


def test_server_refuses_tools_outside_the_export_set(tmp_path):
    server = _server(tmp_path)

    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "write_file", "arguments": {"path": "x", "content": "y"}}}
    )

    assert reply["error"]["code"] == -32602
    assert "not exported" in reply["error"]["message"]


def test_server_keeps_path_anchoring(tmp_path):
    """护栏对外部调用方和内部模型一视同仁——逃逸照样被挡。"""
    server = _server(tmp_path)

    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "../../etc/passwd"}}}
    )

    assert reply["result"]["isError"] is True
    assert "path escapes workspace" in reply["result"]["content"][0]["text"]


def test_server_ignores_notifications(tmp_path):
    assert _server(tmp_path).handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_server_reports_unknown_methods(tmp_path):
    reply = _server(tmp_path).handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})

    assert reply["error"]["code"] == -32601


def test_server_survives_a_malformed_line(tmp_path):
    server = _server(tmp_path)
    stdout = io.StringIO()

    server.serve(stdin=io.StringIO('not json\n{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'), stdout=stdout)

    assert json.loads(stdout.getvalue().strip())["id"] == 1
