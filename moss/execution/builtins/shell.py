"""Shell classification and cancellable command execution."""

import os
import signal
import subprocess
import textwrap
import time

from ..safety import sandbox
from ..safety import shell as shell_policy
from ..specs import ToolRunOutput

def classify_shell_command(command):
    """shell 风险分级。实现下沉到 execution/safety/shell.py。

    注册表保留这个薄入口，避免调用方了解安全策略模块的内部布局；
    真正的分级逻辑是基于 shlex 的结构化解析，
    见 shell_policy 模块头的说明。
    """
    return shell_policy.classify_shell_command(command)


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 60))
    if timeout < 1 or timeout > 600:
        raise ValueError("timeout must be in [1, 600]")
    plan = getattr(context, "sandbox_plan", None)
    wrapped = sandbox.wrap_command(command, plan, workspace=context.root) if plan is not None else None
    returncode, stdout, stderr = run_shell_command(
        wrapped or command,
        cwd=context.root,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=context.shell_env(),
        cancel_token=context.cancel_token,
    )
    content = textwrap.dedent(
        f"""\
        exit_code: {returncode}
        stdout:
        {stdout.strip() or "(empty)"}
        stderr:
        {stderr.strip() or "(empty)"}
        """
    ).strip()
    return ToolRunOutput(
        content=content,
        stdout=stdout,
        stderr=stderr,
        exit_code=returncode,
    )


def _terminate_process_group(process):
    """连同整个进程组一起杀。

    为什么不能只 kill 子进程：命令走 shell=True 起的是一个 shell，
    真正干活的是它的子进程（`pytest`、`npm` 之类）。只杀 shell 会留下孤儿进程
    继续占 CPU、继续写工作区——对 agent 来说是最难排查的一类问题。
    POSIX 上用 killpg，Windows 上没有进程组语义，退回 kill。
    """
    try:
        if hasattr(os, "killpg") and process.pid:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
    except (OSError, ProcessLookupError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def run_shell_command(command, *, cwd, timeout, env, cancel_token=None, poll_interval=0.1):
    """跑一条 shell 命令，返回 (returncode, stdout, stderr)。

    自己轮询而不是直接用 `subprocess.run(timeout=...)`：那样拿不到取消信号，
    用户 Ctrl-C 之后命令还会继续跑到超时为止。
    command 可以是字符串（走 shell）或 argv 列表（沙箱包裹后的形式）。
    """
    popen_kwargs = {"shell": isinstance(command, str)}
    if hasattr(os, "setsid"):
        # 独立进程组，超时/取消时才能整组杀干净。
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(  # noqa: S602 - shell=True 是这个工具的语义本身
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        **popen_kwargs,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = process.communicate(timeout=poll_interval)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            pass
        if cancel_token is not None and cancel_token.is_set():
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            raise RuntimeError("run_shell cancelled")
        if time.monotonic() >= deadline:
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)

