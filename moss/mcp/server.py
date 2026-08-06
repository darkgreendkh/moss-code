"""把 moss 自己暴露成 MCP server（spec-09 §9.2）。

为什么值得做：别的 agent 想读这个仓库时，要么自己实现一遍路径锚定、忽略规则、
脱敏和大输出卸载，要么就直接 `cat` ——前者没人会做，后者没有任何护栏。
把 moss 暴露成 server，等于把这套护栏借给它们。

**所有调用都走 `Moss.execute(ActionRequest)`**（spec-03 §4.3 的唯一入口）：
路径锚定、能力策略、审批、脱敏、trace 对内部模型和外部客户端一视同仁。
默认只暴露只读工具——把写入能力挂到一条 stdio 管道上，就等于把审批交给了
一个我们看不见的调用方。
"""

from __future__ import annotations

import json
import sys

from ..tool_context import ActionRequest
from .client import JSONRPC_VERSION, PROTOCOL_VERSION

SERVER_INFO = {"name": "moss", "version": "0.1"}
# 默认导出的工具。只读，且都是"看仓库"这一类。
DEFAULT_EXPORTED_TOOLS = ("list_files", "read_file", "search_text")


def _json_schema(tool):
    from ..context.prefix import _schema_payload

    properties, required = {}, []
    type_map = {"str": "string", "int": "integer", "list": "array"}
    for name, field in _schema_payload(tool["schema"]).items():
        payload = {"type": type_map.get(str(field["type"]), "string")}
        if not field.get("required", True):
            payload["default"] = field.get("default")
        else:
            required.append(name)
        properties[name] = payload
    return {"type": "object", "properties": properties, "required": required}


class MossMcpServer:
    """一个 stdio JSON-RPC server 门面。`serve()` 阻塞读 stdin 直到 EOF。"""

    def __init__(self, agent, exported_tools=DEFAULT_EXPORTED_TOOLS):
        self.agent = agent
        # 导出集合在构造时冻结：运行期按调用方要求扩容，等于把白名单交给对端。
        self.exported_tools = tuple(
            name for name in exported_tools if name in agent.tools and not agent.tools[name]["risky"]
        )

    def tool_definitions(self):
        return [
            {
                "name": name,
                "description": self.agent.tools[name]["description"],
                "inputSchema": _json_schema(self.agent.tools[name]),
            }
            for name in self.exported_tools
        ]

    def handle(self, message):
        """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不用回。"""
        if not isinstance(message, dict) or message.get("method") is None:
            return None
        method = str(message["method"])
        message_id = message.get("id")
        if message_id is None:
            # 通知（如 notifications/initialized）：按协议不回复。
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except LookupError as exc:
            return self._error(message_id, -32601, str(exc))
        except ValueError as exc:
            return self._error(message_id, -32602, str(exc))
        return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}

    def _dispatch(self, method, params):
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        if method == "tools/list":
            return {"tools": self.tool_definitions()}
        if method == "tools/call":
            return self._call(params)
        raise LookupError(f"unknown method: {method}")

    def _call(self, params):
        name = str(params.get("name", ""))
        if name not in self.exported_tools:
            # 不在导出集合里就是不存在。回一句"未导出"而不是去试着跑它。
            raise ValueError(f"tool {name} is not exported by this server")
        # 唯一入口。护栏对外部调用方和内部模型完全一样。
        result = self.agent.execute(ActionRequest(name=name, args=dict(params.get("arguments") or {})))
        failed = str(result.metadata.get("tool_status", "")) not in {"ok", "partial_success"}
        return {
            "content": [{"type": "text", "text": result.content}],
            "isError": failed,
        }

    @staticmethod
    def _error(message_id, code, text):
        return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "error": {"code": code, "message": text}}

    def serve(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # 对端发了非 JSON。跳过而不是断连：一条坏消息不该终止会话。
                continue
            reply = self.handle(message)
            if reply is None:
                continue
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
