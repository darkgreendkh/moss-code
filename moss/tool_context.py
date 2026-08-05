"""Narrow context passed from runtime into tool functions."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    skills_provider: Callable[[], dict] = lambda: {}
    # 取消令牌：Ctrl-C 之后 run_shell 要能立刻把整个进程组杀掉，
    # 而不是让命令继续跑到超时。None 表示"没人会取消"，行为与以前一致。
    cancel_token: object = None
    # 计划写入回调。计划属于运行时状态（要进 task_state 和 prompt），
    # 而工具函数只认识 ToolContext，所以用回调把写入权交给 runtime。
    plan_writer: Callable[[list], None] = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def skills(self):
        return self.skills_provider() or {}

    def set_plan(self, plan):
        if self.plan_writer is None:
            return
        self.plan_writer(list(plan))


@dataclass(frozen=True)
class ActionRequest:
    """一次结构化的工具执行请求。

    存在的意义是给外部集成（MCP server、hooks、评测）一个明确的入参形状，
    而不是让它们各自拼 dict —— 拼 dict 的下一步往往就是绕过护栏直连 toolkit。
    """

    name: str
    args: dict
    call_id: str = ""
