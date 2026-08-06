"""MCP 客户端与服务端（spec-09 §9.2）。

两个方向：

- **客户端**（`client.py` + `registry.py`）：把外部 MCP server 暴露的工具在
  **启动期**转成 `ToolSpec` 写进注册表。刻意不做运行期动态发现——模型看到的
  动作集合必须是一个有边界、可审计、进 `tool_signature` 的白名单。
- **服务端**（`server.py`）：把 moss 自己暴露成 MCP server，所有调用走
  `Moss.execute(ActionRequest)` 这个唯一入口，护栏一视同仁。

零第三方依赖：JSON-RPC 2.0 over stdio，`subprocess` + `json` 手写。
"""

from .client import McpClient, McpError, StdioTransport
from .registry import (
    DEFAULT_CATALOG_THRESHOLD,
    McpServerConfig,
    build_mcp_tools,
    load_mcp_config,
    mcp_tool_name,
)

__all__ = [
    "McpClient",
    "McpError",
    "StdioTransport",
    "McpServerConfig",
    "build_mcp_tools",
    "load_mcp_config",
    "mcp_tool_name",
    "DEFAULT_CATALOG_THRESHOLD",
]
