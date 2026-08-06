"""MCP command implementation."""

import argparse

from ...config import load_project_env
from ...context.repository.workspace import WorkspaceContext
from ...extensions.mcp.server import DEFAULT_EXPORTED_TOOLS, MossMcpServer
from ...providers.clients import FakeModelClient
from ...runs.session import SessionStore
from ...runtime import Moss

def build_mcp_arg_parser():
    parser = argparse.ArgumentParser(prog="moss mcp", description="Expose moss over MCP.")
    commands = parser.add_subparsers(dest="mcp_command", required=True)
    serve_parser = commands.add_parser("serve", help="Serve read-only workspace tools over MCP stdio.")
    serve_parser.add_argument("--cwd", default=".", help="Workspace to expose.")
    serve_parser.add_argument(
        "--tools",
        default=",".join(DEFAULT_EXPORTED_TOOLS),
        help="Comma-separated tools to export. Risky tools are dropped regardless.",
    )
    return parser


def run_mcp_command(argv):
    args = build_mcp_arg_parser().parse_args(argv)
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    agent = Moss(
        # server 模式下没有模型调用，只有工具执行——所以给一个空 client。
        # 别让"暴露只读工具"这件事顺带需要一把 API key。
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(workspace.repo_root + "/.moss/sessions"),
        approval_policy="never",
        read_only=True,
    )
    tools = tuple(name.strip() for name in str(args.tools).split(",") if name.strip())
    MossMcpServer(agent, exported_tools=tools).serve()
    return 0

