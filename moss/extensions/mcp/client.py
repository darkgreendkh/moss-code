"""MCP stdio 客户端：JSON-RPC 2.0，stdlib 手写（spec-09 §9.2）。

为什么手写：本项目零第三方运行时依赖。MCP 的 stdio 传输就是"一行一个 JSON-RPC
消息"，加上 initialize / tools/list / tools/call 三个方法，几百行就够了。

**server 是外部进程，它的输出是不可信数据。** 这里只负责把字节变成 dict；
"这段文本会不会试图指挥模型"由 `ToolExecutor` 的注入扫描和 `<tool_result
untrusted>` 包裹负责——那条链路对内置工具和 MCP 工具一视同仁。
"""

from __future__ import annotations

import json
import subprocess
import threading

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "moss", "version": "0.1"}
# 单次请求超时。外部进程挂住不能把整个 agent 拖死。
DEFAULT_TIMEOUT_S = 30


class McpError(RuntimeError):
    """server 返回了 JSON-RPC error，或者根本没答上来。"""


class StdioTransport:
    """把子进程的 stdin/stdout 当成一条 JSON-RPC 管道。

    换行分隔（MCP stdio 的约定）：每条消息一行 JSON，读到 EOF 就是对端没了。
    """

    def __init__(self, command, *, cwd=None, env=None, timeout=DEFAULT_TIMEOUT_S):
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.process = None
        # 一个 client 可能被多线程用（只读工具批可以并发）。JSON-RPC 的
        # id 匹配在单管道上做起来要一整套 pending 表；这里用锁把请求串行化，
        # 简单而且足够——MCP 调用本来就是外部进程，延迟由它主导。
        self._lock = threading.Lock()

    def start(self):
        if self.process is not None:
            return self.process
        self.process = subprocess.Popen(  # noqa: S603 - 命令来自用户自己的 .moss/config.json
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.cwd,
            env=self.env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        return self.process

    def close(self):
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=5)
        except Exception:
            process.kill()

    def roundtrip(self, payload, *, expect_reply=True):
        process = self.start()
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                process.stdin.write(line)
                process.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise McpError(f"mcp server closed its stdin: {exc}") from exc
            if not expect_reply:
                return None
            while True:
                reply = process.stdout.readline()
                if not reply:
                    raise McpError("mcp server closed its stdout without replying")
                reply = reply.strip()
                if not reply:
                    continue
                try:
                    message = json.loads(reply)
                except json.JSONDecodeError:
                    # server 往 stdout 打了非协议内容（很常见的实现 bug）。
                    # 跳过它继续读，而不是把整个连接判死。
                    continue
                if isinstance(message, dict) and "id" in message:
                    return message


class McpClient:
    """一个 MCP server 连接。启动期 handshake + 列工具，之后按需调用。"""

    def __init__(self, transport, *, name=""):
        self.transport = transport
        self.name = str(name or "")
        self._next_id = 0
        self.server_info = {}

    def _request(self, method, params=None):
        self._next_id += 1
        message = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        reply = self.transport.roundtrip(message)
        if not isinstance(reply, dict):
            raise McpError(f"mcp {method}: malformed reply")
        if reply.get("error"):
            error = reply["error"]
            raise McpError(f"mcp {method} failed: {error.get('message', error)}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method, params=None):
        self.transport.roundtrip(
            {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}},
            expect_reply=False,
        )

    def initialize(self):
        self.server_info = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._notify("notifications/initialized")
        return self.server_info

    def list_tools(self):
        """列出 server 暴露的工具。**只在启动期调用一次。**

        运行期不再重列：模型看到的动作集合在 run 内必须是冻结的，否则
        `tool_signature` 会在同一个任务中途变化，prompt cache 跟着抖。
        """
        tools = self._request("tools/list").get("tools", [])
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    def call_tool(self, name, arguments):
        result = self._request("tools/call", {"name": str(name), "arguments": dict(arguments or {})})
        text = flatten_content(result.get("content", []))
        if result.get("isError"):
            raise McpError(text or f"mcp tool {name} reported an error")
        return text

    def close(self):
        self.transport.close()


def flatten_content(content):
    """MCP 的 content 是 block 列表。把它拍平成文本。

    非 text block（图片、资源引用）只留一行类型说明——把 base64 塞进 prompt
    既没用又能一口气吃掉整个预算。
    """
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", ""))
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(f"[resource {resource.get('uri', '?')}]")
        else:
            parts.append(f"[{kind or 'unknown'} content omitted]")
    return "\n".join(part for part in parts if part)
