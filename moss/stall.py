"""停滞检测。

为什么存在：agent 卡住的方式远不止"反复发同一个调用"这一种。它还会在两个
工具之间来回横跳、连续读一堆文件却什么都没改、或者对着同一个错误反复重试。
原来的护栏只比较最近两条工具事件，上面三种一个都拦不住，只能等步数上限兜底
——那意味着整份预算已经烧完了。

命中之后不是简单拒绝执行，而是注入一条**结构化**干预：告诉模型它在重复什么、
已知的失败原因是什么、可以换哪条路。单纯的"拒绝"只会让它换个参数再试一次。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# 检测窗口：只看最近 N 条工具事件。太长会把任务早期的正常探索也算进来。
DEFAULT_WINDOW = 8

# 各类停滞的判定阈值。
REPEAT_EXACT_THRESHOLD = 3
AB_LOOP_REPEATS = 2
NO_PROGRESS_STEPS = 4
ERROR_STORM_THRESHOLD = 3


@dataclass(frozen=True)
class StallSignal:
    kind: str      # repeat_exact | ab_loop | no_progress | error_storm
    detail: str
    window: int

    def notice(self):
        """给模型的结构化干预文本。"""
        return (
            f"Runtime notice: you appear to be stuck ({self.kind}). {self.detail} "
            "Try a different approach, or return a <final> answer explaining what is blocking you."
        )


def args_digest(args):
    """把工具参数压成一个可比对的短指纹。

    用 digest 而不是原始 dict：参数里可能有大段文件内容，
    直接放进窗口比对既占内存又慢，而这里只需要"是不是同一次调用"。
    """
    try:
        payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _signature(event):
    return (str(event.get("name", "")), args_digest(event.get("args")))


def _detect_repeat_exact(signatures):
    counts = {}
    for signature in signatures:
        counts[signature] = counts.get(signature, 0) + 1
    for signature, count in counts.items():
        if count >= REPEAT_EXACT_THRESHOLD:
            return StallSignal(
                kind="repeat_exact",
                detail=f"The call to {signature[0]} with the same arguments has run {count} times with no new information.",
                window=len(signatures),
            )
    return None


def _detect_ab_loop(signatures):
    """A→B→A→B 这类环。2-gram 和 3-gram 各查一遍。"""
    for size in (2, 3):
        if len(signatures) < size * AB_LOOP_REPEATS:
            continue
        tail = signatures[-size * AB_LOOP_REPEATS :]
        chunks = [tuple(tail[index : index + size]) for index in range(0, len(tail), size)]
        if len(set(chunks)) == 1 and len(set(chunks[0])) > 1:
            names = " → ".join(name for name, _ in chunks[0])
            return StallSignal(
                kind="ab_loop",
                detail=f"You are cycling between the same calls: {names}.",
                window=len(signatures),
            )
    return None


def _detect_no_progress(events):
    """连续若干步没改动工作区，且没读到任何新路径。

    单看"没改动"会误伤正常的调研阶段——读文件本来就不该改工作区。
    所以还要求读到的路径集合没有新增：既没产出，也没获得新信息。
    """
    if len(events) < NO_PROGRESS_STEPS:
        return None
    window = events[-NO_PROGRESS_STEPS:]
    if any(event.get("workspace_changed") for event in window):
        return None
    seen_before = set()
    for event in events[: -NO_PROGRESS_STEPS]:
        path = str((event.get("args") or {}).get("path", "")).strip()
        if path:
            seen_before.add(path)
    for event in window:
        path = str((event.get("args") or {}).get("path", "")).strip()
        if path and path not in seen_before:
            return None
    return StallSignal(
        kind="no_progress",
        detail=f"The last {NO_PROGRESS_STEPS} tool calls changed nothing and surfaced no new files.",
        window=len(events),
    )


def _detect_error_storm(events):
    codes = [str(event.get("tool_error_code", "") or "") for event in events]
    streak = 0
    last_code = ""
    for code in codes:
        if code and code == last_code:
            streak += 1
        elif code:
            streak = 1
            last_code = code
        else:
            streak = 0
            last_code = ""
        if streak >= ERROR_STORM_THRESHOLD:
            return StallSignal(
                kind="error_storm",
                detail=f"The same failure ({last_code}) has happened {streak} times in a row.",
                window=len(events),
            )
    return None


def detect_stall(events, *, window=DEFAULT_WINDOW):
    """在最近 window 条工具事件里找停滞信号，没有就返回 None。

    events 里每一项形如 {name, args, workspace_changed, tool_error_code}。
    检测顺序即优先级：越具体的信号越先报，因为它给出的干预更有针对性。
    """
    events = [dict(event) for event in events][-window:]
    if not events:
        return None
    signatures = [_signature(event) for event in events]
    for signal in (
        _detect_repeat_exact(signatures),
        _detect_error_storm(events),
        _detect_ab_loop(signatures),
        _detect_no_progress(events),
    ):
        if signal is not None:
            return signal
    return None


def is_repeated_call(events, name, args):
    """现有 `repeated_tool_call` 的语义：最近两次都是同一个调用。

    保留它是为了不破坏既有行为和测试——它相当于 repeat_exact 的一个特例，
    只是判定更早（在执行前）也更严格（必须紧邻）。
    """
    tool_events = [event for event in events if event.get("role", "tool") == "tool"]
    if len(tool_events) < 2:
        return False
    target = (str(name), args_digest(args))
    return all(_signature(event) == target for event in tool_events[-2:])
