"""把 MCP server 的工具在启动期落进白名单（spec-09 §9.2）。

三条不可动摇的规则：

1. **注册期显式落白名单，不做运行期动态发现。** MCP server 中途多暴露一个工具
   不该让模型在同一个 run 里突然多出一项能力——那既绕过了 `tool_signature`，
   也让 prompt cache 在任务中途失效。
2. **外部工具必须声明 capabilities，否则 fail-closed 拒绝。** 一个不知道自己
   会干什么的外部进程，默认就该是"不许跑"。
3. **一律 risky + 隐含 network。** MCP server 是外部进程，它能连什么、写什么，
   我们证明不了。证明不了就按最坏算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import project_config_section
from ..policy import CAPABILITIES
from ..tools import ToolField, ToolSpec
from .client import McpClient, McpError, StdioTransport

# 工具名前缀。加前缀是为了让 MCP 工具在 trace / 审批摘要 / 报错里一眼可辨，
# 也避免外部 server 用 `read_file` 之类的名字把内置工具顶掉。
MCP_TOOL_PREFIX = "mcp__"

# 超过这个工具数就把 prefix 切成"名字 + 一句话"目录，schema 改由 describe_tool 取。
# 为什么不是 spec 里写的 12：内置注册表本身就有 14 个工具，取 12 会让**每一次**
# 默认运行都切进目录模式——这个开关是给"接了外部 server 之后工具数膨胀"用的，
# 不是用来改默认渲染的。
DEFAULT_CATALOG_THRESHOLD = 16

_JSON_TYPE_TO_FIELD = {
    "string": "str",
    "integer": "int",
    "number": "int",
    "array": "list",
    "boolean": "str",
    "object": "str",
}


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: tuple
    capabilities: frozenset
    # 只注册这些工具；空表示 server 暴露什么就注册什么（仍然是启动期一次性快照）。
    tools: tuple = ()
    env: dict = field(default_factory=dict)
    timeout_s: int = 30

    @classmethod
    def parse(cls, name, payload):
        """解析一条 server 配置。缺 capabilities 直接拒——这是 fail-closed 的入口。"""
        if not isinstance(payload, dict):
            raise ValueError(f"mcp server {name}: config must be a mapping")
        command = payload.get("command")
        if isinstance(command, str):
            command = [command]
        if not command or not all(isinstance(item, str) for item in command):
            raise ValueError(f"mcp server {name}: command must be a non-empty list of strings")
        if "capabilities" not in payload:
            raise ValueError(
                f"mcp server {name}: must declare capabilities "
                f"(one or more of {sorted(CAPABILITIES)}); external tools are fail-closed"
            )
        declared = {str(item) for item in payload.get("capabilities") or ()}
        unknown = sorted(declared - CAPABILITIES)
        if unknown:
            raise ValueError(f"mcp server {name}: unknown capabilities: {', '.join(unknown)}")
        return cls(
            name=str(name),
            command=tuple(command),
            # network 是隐含的：外部进程能连什么我们证明不了，证明不了就按最坏算。
            capabilities=frozenset(declared | {"network"}),
            tools=tuple(str(item) for item in payload.get("tools") or ()),
            env=dict(payload.get("env") or {}),
            timeout_s=int(payload.get("timeout_s", 30)),
        )


def load_mcp_config(root):
    """读 `.moss/config.json` 的 `mcp` 段。

    返回 `(servers, errors, catalog_threshold)`。坏配置不抛异常——一条 server
    写错了不该让 agent 起不来，但也绝不能静默放行：错误列表由调用方打到 stderr。
    """
    section = project_config_section(root, "mcp")
    servers, errors = [], []
    for name, payload in sorted((section.get("servers") or {}).items()):
        try:
            servers.append(McpServerConfig.parse(name, payload))
        except ValueError as exc:
            errors.append(str(exc))
    threshold = int(section.get("catalog_threshold", DEFAULT_CATALOG_THRESHOLD) or DEFAULT_CATALOG_THRESHOLD)
    return servers, errors, threshold


def mcp_tool_name(server_name, tool_name):
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}"


def fields_from_json_schema(schema):
    """把 MCP 的 inputSchema 变成 moss 的 ToolField 表。

    只认标量与数组。认不出来的类型退成字符串——比拒绝注册友好，
    也比假装认识它安全（校验层照样会按字符串检查）。
    """
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in schema.get("required") or ()}
    fields = {}
    for key, definition in properties.items():
        definition = definition if isinstance(definition, dict) else {}
        json_type = str(definition.get("type", "string"))
        field_type = _JSON_TYPE_TO_FIELD.get(json_type, "str")
        is_required = str(key) in required
        default = definition.get("default")
        if not is_required and default is None:
            default = [] if field_type == "list" else ("" if field_type == "str" else 0)
        fields[str(key)] = ToolField(field_type, required=is_required, default=default)
    return fields


def spec_from_mcp_tool(server, tool):
    description = str(tool.get("description", "")).strip() or f"{server.name} tool"
    return ToolSpec(
        name=mcp_tool_name(server.name, tool["name"]),
        fields=fields_from_json_schema(tool.get("inputSchema")),
        # 外部进程一律 risky：它做了什么我们只能听它自己说。
        risky=True,
        capabilities=server.capabilities,
        description=f"[mcp:{server.name}] {description}",
    )


def build_mcp_tools(root, *, connect=None, on_error=None):
    """启动期连上所有配置好的 MCP server，把它们的工具变成注册表条目。

    返回 `(entries, clients, threshold)`。`entries` 的形状和
    `tools.build_tool_registry()` 的一致，可以直接并进去。

    连不上不抛异常：一个装不上的 server 不该让 agent 起不来。但失败必须
    经 `on_error` 说出来——静默少几个工具，表现是模型莫名其妙做不成事。
    """
    servers, errors, threshold = load_mcp_config(root)
    report = on_error or (lambda message: None)
    for message in errors:
        report(message)

    entries, clients = {}, []
    for server in servers:
        try:
            client = (connect or _connect)(server)
            tools = client.list_tools()
        except (McpError, OSError, ValueError) as exc:
            report(f"mcp server {server.name}: unavailable ({exc})")
            continue
        clients.append(client)
        wanted = set(server.tools)
        for tool in tools:
            if wanted and tool["name"] not in wanted:
                continue
            spec = spec_from_mcp_tool(server, tool)
            entries[spec.name] = _entry(spec, client, tool["name"])
    return entries, clients, threshold


def _connect(server):
    client = McpClient(
        StdioTransport(server.command, env=server.env or None, timeout=server.timeout_s),
        name=server.name,
    )
    client.initialize()
    return client


def _entry(spec, client, remote_name):
    def run(args):
        return client.call_tool(remote_name, args or {})

    return {
        "schema": dict(spec.fields),
        "risky": spec.risky,
        "description": spec.description,
        "capabilities": frozenset(spec.capabilities),
        "path_scope": spec.path_scope,
        "spec": spec,
        "run": run,
    }
