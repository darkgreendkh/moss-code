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

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def skills(self):
        return self.skills_provider() or {}
