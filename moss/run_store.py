"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

import hashlib
import json
from pathlib import Path

from .atomic_io import write_atomic, write_json_atomic
from .lease import RunLease
from .task_state import STATUS_RUNNING, TaskState

# 哈希链的起点。第一条事件的 prev_hash 用它，链条才有明确的头。
TRACE_CHAIN_GENESIS = "0" * 64


def event_digest(event):
    """一条事件的规范化摘要。

    canonical json（排序键 + 固定分隔符）是关键：字段顺序或空格变一下就算出
    另一个 hash 的话，链条校验会变成"格式检查"而不是"内容检查"。
    """
    payload = {key: value for key, value in dict(event or {}).items() if key != "prev_hash"}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    def __init__(self, root, lease=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # 租约：谁在跑这个 run。没有它，启动时的"把所有 running 标成 interrupted"
        # 会在并发下杀掉另一个终端正在进行的任务（spec-07 §1 的 bug #2）。
        self.lease = lease if lease is not None else RunLease(self.run_dir)

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def artifacts_dir(self, run_id):
        """大工具输出的卸载目录。

        为什么和 trace 分开：trace 是"发生了什么"的时间线，artifacts 是
        "那一步的完整输出"。前者要能整份读进内存，后者可能是几 MB 的 pytest 日志。
        """
        return self.run_dir(run_id) / "artifacts"

    def context_dir(self, run_id):
        """被 compaction 压缩掉的原始历史。摘要里附路径，模型可用 read_artifact 取回。"""
        return self.run_dir(run_id) / "context"

    def write_context_turns(self, run_id, index, entries):
        """把被 compaction 压掉的原始历史整份存下来，返回 (相对路径, 条数)。

        没有这一步，压缩就是又一种有损截断：模型看到摘要说"读过 parser.py"，
        却再也拿不回当时到底读到了什么。
        """
        directory = self.context_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"turns-{int(index)}.jsonl"
        entries = list(entries or [])
        body = "".join(
            json.dumps(entry, sort_keys=True, ensure_ascii=True) + "\n" for entry in entries
        )
        self._write_text_atomic(path, body)
        return f"context/{path.name}", len(entries)

    def write_artifact(self, run_id, sequence, tool, text):
        """把一份大输出落盘，返回 (相对 run 目录的路径, 行数)。

        文件名带内容 sha12，并且先按 sha12 找现成的：同一份 pytest 输出被读三次
        只该占一份盘，也让"同一内容 = 同一指针"，模型不会以为拿到了不同的东西。
        """
        text = str(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        directory = self.artifacts_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        lines = len(text.splitlines()) or 1
        existing = sorted(directory.glob(f"*-{digest}.txt"))
        if existing:
            return f"artifacts/{existing[0].name}", lines
        safe_tool = "".join(ch for ch in str(tool) if ch.isalnum() or ch in "-_") or "tool"
        path = directory / f"{int(sequence):03d}-{safe_tool}-{digest}.txt"
        self._write_text_atomic(path, text)
        return f"artifacts/{path.name}", lines

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        # 租约先于任何执行：run 目录一旦是 running，别的进程就可能来接管它，
        # 这段窗口里必须已经能证明"我还活着"。
        self.lease.acquire(_run_id(task_state))
        return run_dir

    def heartbeat(self, run_id):
        """刷新租约心跳。主循环每步调用，长工具期间由心跳线程调用。"""
        return self.lease.heartbeat(_run_id(run_id))

    def release_run(self, run_id):
        """释放租约。收尾路径（含中断）必须调用，绝不抛异常。"""
        return self.lease.release(_run_id(run_id))

    def write_task_state(self, task_state):
        return self.write_task_state_payload(task_state, task_state.to_dict())

    def write_task_state_payload(self, run_id, payload):
        path = self.task_state_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, payload)
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = self._trace_event(task_state, event)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path

    def verify_trace(self, run_id):
        """逐条校验哈希链，返回 (是否完整, 问题列表)。

        它能发现的是"事后被改过/被删过"：trace 是审计工件，
        如果谁能悄悄改掉一条记录，整份工件就失去了证据价值。
        """
        events = self.read_trace(run_id)
        problems = []
        previous = TRACE_CHAIN_GENESIS
        for index, event in enumerate(events):
            expected_sequence = index + 1
            if int(event.get("sequence", 0) or 0) != expected_sequence:
                problems.append(f"sequence gap at index {index}: expected {expected_sequence}")
            if "prev_hash" not in event:
                problems.append(f"event {event.get('event_id', index)} has no prev_hash")
                previous = event_digest(event)
                continue
            if event.get("prev_hash") != previous:
                problems.append(f"broken chain at {event.get('event_id', index)}")
            previous = event_digest(event)
        return (not problems), problems

    def read_trace(self, run_id):
        path = self.trace_path(run_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        events = []
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                events.append(json.loads(text))
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    continue
                raise
        return sorted(events, key=lambda item: int(item.get("sequence", 0) or 0))

    def find_running_runs(self):
        running = []
        for task_path in sorted(self.root.glob("*/task_state.json")):
            try:
                payload = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(payload.get("status", "")) == STATUS_RUNNING:
                running.append(TaskState.from_dict(payload))
        return running

    def mark_interrupted_runs(self):
        """接管别的进程留下的、已经判死的 run。

        为什么不再"看到 running 就标 interrupted"：那在并发下是静默数据损坏——
        另一个终端的 REPL 正跑着，这边一启动就把它的 task_state 覆写成 failed，
        用户看到任务无缘无故失败，而 trace 里根本没有对应的失败原因。
        现在先按租约判活，判不出来一律当活着（保守方向：最坏是该接管的没接管）。

        两种接管结论分开记，不混算：
        - `interrupted`：有租约且判死，确认是"跑到一半没了"；
        - `stale`：连租约文件都没有（旧版本留下的，或写租约之前就崩了），
          我们并不知道它是怎么没的，不该计进"中断率"这类指标。
        """
        interrupted = []
        for task_state in self.find_running_runs():
            can_take_over, takeover = self.lease.classify(task_state.run_id)
            if not can_take_over:
                continue
            events = self.read_trace(task_state.run_id)
            last_event = events[-1] if events else {}
            task_state.stop_interrupted("Run interrupted before completion.")
            self.write_task_state(task_state)
            self.write_report(
                task_state,
                {
                    "run_id": task_state.run_id,
                    "task_id": task_state.task_id,
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": task_state.final_answer,
                    "task_state": task_state.to_dict(),
                    "last_complete_event": last_event,
                    "takeover": takeover,
                },
            )
            self.lease.release(task_state.run_id)
            interrupted.append(
                {
                    "run_id": task_state.run_id,
                    "task_id": task_state.task_id,
                    "last_complete_event": last_event,
                    "takeover": takeover,
                }
            )
        return interrupted

    def active_runs(self):
        """持有有效租约的 run。保留策略永不清理它们。"""
        active = []
        for task_state in self.find_running_runs():
            if self.lease.is_alive(self.lease.read(task_state.run_id)):
                active.append(task_state.run_id)
        return active

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        return path

    def load_task_state(self, task_id):
        return json.loads(self.task_state_path(task_id).read_text(encoding="utf-8"))

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))

    def _trace_event(self, task_state, event):
        payload = dict(event or {})
        run_id = _run_id(task_state)
        sequence = self._next_trace_sequence(run_id)
        payload["sequence"] = sequence
        payload["event_id"] = f"{run_id}:{sequence:06d}"
        # 每条事件带上一条的摘要：改掉中间任何一条，后面全部对不上。
        payload["prev_hash"] = self._last_trace_hash(run_id)
        payload.setdefault("run_id", run_id)
        if hasattr(task_state, "task_id"):
            payload.setdefault("task_id", task_state.task_id)
            payload.setdefault("attempt", task_state.attempts)
            payload.setdefault("tool_steps", task_state.tool_steps)
        else:
            payload.setdefault("task_id", "")
            payload.setdefault("attempt", int(payload.get("attempt", 0) or 0))
            payload.setdefault("tool_steps", int(payload.get("tool_steps", 0) or 0))
        return payload

    def _last_trace_hash(self, run_id):
        events = self.read_trace(run_id)
        if not events:
            return TRACE_CHAIN_GENESIS
        return event_digest(events[-1])

    def _next_trace_sequence(self, run_id):
        events = self.read_trace(run_id)
        if not events:
            return 1
        return max(int(event.get("sequence", 0) or 0) for event in events) + 1

    def _write_text_atomic(self, path, text):
        # 和 _write_json_atomic 同样的理由：半截 artifact 比没有 artifact 更难排查，
        # 而模型会照着指针去读它。原子 + fsync 都在 atomic_io 里统一实现。
        return write_atomic(path, text)

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件、fsync，再 replace。
        # 这样即使中途断电，也不会留下半截 JSON。
        return write_json_atomic(path, payload, ensure_ascii=True)
