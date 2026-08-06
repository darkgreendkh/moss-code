"""用户钩子（spec-09 §9.5）。

钩子点：`pre_tool` / `post_tool` / `pre_final` / `post_run`，对应
`.moss/hooks/<point>` 这个可执行文件（也接受 `.sh` / `.py` 后缀）。

三条纪律：

1. **超时 3 秒，失败不阻断控制流。** 沿用 `progress_observer` 的既有纪律：
   一个写坏的钩子不该让 agent 停摆。唯一的例外是下面第 2 条。
2. **`pre_tool` 用退出码 2 表达"拒绝这次调用"。** 这是唯一能影响控制流的钩子，
   所以它必须在 trace 里留下 `hook_denied`——一次被悄悄拒掉的工具调用，
   表现是模型莫名其妙地绕圈。
3. **钩子拿到的是脱敏后的 JSON**（stdin）。钩子是用户的脚本，不是可信执行环境；
   把原始 secret 递给它等于把脱敏边界往外挪了一格。

**agent 不能写 `.moss/`**（`policy.DEFAULT_DENY` 覆盖了这条）。否则 agent
能往这里塞一个自己的 `pre_tool`，给自己装后门。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOOKS_SUBDIR = ".moss/hooks"

PRE_TOOL = "pre_tool"
POST_TOOL = "post_tool"
PRE_FINAL = "pre_final"
POST_RUN = "post_run"
HOOK_POINTS = (PRE_TOOL, POST_TOOL, PRE_FINAL, POST_RUN)

# 超时 3 秒。钩子的正当用途是"跑个 ruff""跑个快测试"，超过这个量级
# 就该由 agent 自己用 run_shell 跑，而不是挂在每次工具调用上。
HOOK_TIMEOUT_S = 3
# `pre_tool` 用它表达拒绝。别的退出码一律当"钩子自己失败了"，不影响控制流。
DENY_EXIT_CODE = 2

_SUFFIXES = ("", ".sh", ".py")


class HookOutcome:
    """一次钩子执行的结果。`denied` 只可能来自 `pre_tool`。"""

    __slots__ = ("point", "ran", "denied", "exit_code", "stdout", "stderr", "error")

    def __init__(self, point, *, ran=False, denied=False, exit_code=None, stdout="", stderr="", error=""):
        self.point = point
        self.ran = ran
        self.denied = denied
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    def to_dict(self):
        return {
            "point": self.point,
            "ran": self.ran,
            "denied": self.denied,
            "exit_code": self.exit_code,
            "reason": (self.stderr or self.stdout or self.error).strip()[:400],
        }


def find_hook(root, point):
    """找到某个钩子点对应的可执行文件。找不到返回 None。

    只认可执行位：一个没加 +x 的脚本更可能是半成品，拿 `sh` 去跑它
    等于替用户做了一个他没表达过的决定。
    """
    if point not in HOOK_POINTS:
        raise ValueError(f"unknown hook point: {point}")
    directory = Path(root) / HOOKS_SUBDIR
    for suffix in _SUFFIXES:
        candidate = directory / f"{point}{suffix}"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def run_hook(root, point, payload, *, env=None, timeout=HOOK_TIMEOUT_S):
    """跑一个钩子。任何异常都收敛成 `HookOutcome`，绝不上抛。

    `payload` 必须是**已经脱敏**的 dict——脱敏发生在调用方，因为只有它
    知道该用哪份 secret 名单。
    """
    hook = find_hook(root, point)
    if hook is None:
        return HookOutcome(point)
    try:
        result = subprocess.run(  # noqa: S603 - 钩子是用户自己放进 .moss/hooks 的
            [str(hook)],
            input=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # 超时不阻断：一个挂住的钩子不该把 agent 一起挂住。
        return HookOutcome(point, ran=True, error=f"hook timed out after {timeout}s")
    except OSError as exc:
        return HookOutcome(point, ran=True, error=f"hook could not run: {exc}")
    return HookOutcome(
        point,
        ran=True,
        # 只有 pre_tool 的退出码 2 有拒绝语义。别的钩子退 2 也只是它自己失败了。
        denied=point == PRE_TOOL and result.returncode == DENY_EXIT_CODE,
        exit_code=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )
