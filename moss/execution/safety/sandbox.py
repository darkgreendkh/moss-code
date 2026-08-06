"""分层沙箱。

为什么分层：能用的隔离手段各平台不一样，而"用不上"不该等于"跑不了"。
所以从策略层（永远可用）到 OS 沙箱（能探测到就用）再到容器（用户显式要求），
逐层加码。

**降级必须显式**：探测不到 bwrap 就静默地什么都不做，是最危险的一种实现——
用户以为自己开了沙箱。所以每次降级都会写进 report 的 sandbox 字段，
并在 stderr 上说一次。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

# 由 --sandbox 指定。auto 表示"能用哪层用哪层"。
SANDBOX_MODES = ("off", "auto", "sandbox-exec", "bwrap", "docker", "podman")


@dataclass(frozen=True)
class SandboxPlan:
    """这次运行实际用上的隔离手段。"""

    mode: str = "none"          # none | sandbox-exec | bwrap | docker | podman
    requested: str = "auto"
    degraded: bool = False
    reason: str = ""

    def describe(self):
        if self.mode == "none":
            return f"sandbox=none (requested {self.requested}): {self.reason or 'no sandbox available'}"
        return f"sandbox={self.mode}"

    def to_dict(self):
        return {
            "mode": self.mode,
            "requested": self.requested,
            "degraded": self.degraded,
            "reason": self.reason,
        }


def _container_available(runtime):
    if shutil.which(runtime) is None:
        return False, f"{runtime} is not installed"
    try:
        result = subprocess.run(
            [runtime, "info"], capture_output=True, text=True, timeout=10, check=False
        )
    except Exception as exc:
        return False, f"{runtime} info failed: {exc}"
    if result.returncode != 0:
        return False, f"{runtime} daemon is not reachable"
    return True, ""


def detect(requested="auto", *, platform=None):
    """决定这次运行用哪一层，并把降级原因记清楚。"""
    requested = str(requested or "auto")
    platform = platform or sys.platform
    if requested == "off":
        return SandboxPlan(mode="none", requested=requested, degraded=False, reason="disabled by request")

    if requested in {"docker", "podman"}:
        available, reason = _container_available(requested)
        if available:
            return SandboxPlan(mode=requested, requested=requested)
        # 用户显式要了容器却用不了：这是错误，不是降级。静默退回宿主机执行
        # 等于把用户明确表达的隔离意图丢掉。
        raise RuntimeError(f"--sandbox={requested} was requested but is unusable: {reason}")

    if requested == "sandbox-exec" or (requested == "auto" and platform == "darwin"):
        if shutil.which("sandbox-exec"):
            return SandboxPlan(mode="sandbox-exec", requested=requested)
        if requested == "sandbox-exec":
            return SandboxPlan(
                mode="none", requested=requested, degraded=True, reason="sandbox-exec not found"
            )

    if requested == "bwrap" or (requested == "auto" and platform.startswith("linux")):
        if shutil.which("bwrap"):
            return SandboxPlan(mode="bwrap", requested=requested)
        if requested == "bwrap":
            return SandboxPlan(mode="none", requested=requested, degraded=True, reason="bwrap not found")

    return SandboxPlan(
        mode="none",
        requested=requested,
        degraded=requested != "auto",
        reason="no supported sandbox on this platform",
    )


def wrap_command(command, plan, *, workspace):
    """把命令包进沙箱。plan.mode 为 none 时原样返回。

    写入范围限制在工作区 + 临时目录：agent 该改的只有它自己的仓库，
    改到 ~/.ssh 或 /usr/local 属于越界，不是"任务需要"。
    """
    workspace = str(workspace)
    if plan.mode == "sandbox-exec":
        profile = (
            "(version 1)"
            "(allow default)"
            "(deny file-write*)"
            f'(allow file-write* (subpath "{workspace}") (subpath "/private/tmp") (subpath "/tmp"))'
        )
        return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", command]
    if plan.mode == "bwrap":
        return [
            "bwrap",
            "--ro-bind", "/", "/",
            "--bind", workspace, workspace,
            "--bind", "/tmp", "/tmp",
            "--dev", "/dev",
            "--chdir", workspace,
            "/bin/sh", "-c", command,
        ]
    if plan.mode in {"docker", "podman"}:
        return [
            plan.mode, "run", "--rm",
            "--network=none",
            "--user", "1000:1000",
            "-v", f"{workspace}:{workspace}",
            "-w", workspace,
            "alpine:latest",
            "/bin/sh", "-c", command,
        ]
    return None


def announce(plan, stream=None):
    """降级要说出来。用户以为自己开了沙箱、实际没开，是最危险的状态。"""
    stream = stream or sys.stderr
    if plan.degraded:
        print(f"warning: {plan.describe()}", file=stream)
    return plan
