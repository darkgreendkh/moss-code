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
    # 记忆工具只拿到四个窄回调，不持有整个 Moss runtime，避免工具层绕过
    # runtime 里的来源标注、注入扫描和 session 落盘。
    memory_writer: Callable[[dict], dict] = None
    memory_updater: Callable[[dict], dict] = None
    memory_deleter: Callable[[dict], dict] = None
    memory_searcher: Callable[[dict], list] = None
    # 沙箱计划。None 表示不包裹，行为与加沙箱前一致。
    sandbox_plan: object = None
    # run 目录内的路径锚定（read_artifact 用）。和 path_resolver 分开是刻意的：
    # 两者的根不同，混用会让"只能读本次运行的工件"这条约束失效。
    run_path_resolver: Callable[[str], Path] = None
    # 点亮 skill：供应链确认 + 能力临时覆盖都是有状态的，只能由 runtime 做。
    skill_activator: Callable[[str], str] = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def run_path(self, raw_path):
        if self.run_path_resolver is None:
            raise ValueError("no active run directory")
        return self.run_path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def skills(self):
        return self.skills_provider() or {}

    def activate_skill(self, name):
        if self.skill_activator is None:
            raise RuntimeError("use_skill is unavailable")
        return self.skill_activator(str(name))

    def set_plan(self, plan):
        if self.plan_writer is None:
            return
        self.plan_writer(list(plan))

    def write_memory(self, args):
        if self.memory_writer is None:
            raise RuntimeError("memory_write is unavailable")
        return self.memory_writer(dict(args))

    def update_memory(self, args):
        if self.memory_updater is None:
            raise RuntimeError("memory_update is unavailable")
        return self.memory_updater(dict(args))

    def delete_memory(self, args):
        if self.memory_deleter is None:
            raise RuntimeError("memory_delete is unavailable")
        return self.memory_deleter(dict(args))

    def search_memory(self, args):
        if self.memory_searcher is None:
            raise RuntimeError("memory_search is unavailable")
        return self.memory_searcher(dict(args))


@dataclass(frozen=True)
class ActionRequest:
    """一次结构化的工具执行请求。

    存在的意义是给外部集成（MCP server、hooks、评测）一个明确的入参形状，
    而不是让它们各自拼 dict —— 拼 dict 的下一步往往就是绕过护栏直连 toolkit。
    """

    name: str
    args: dict
    call_id: str = ""
