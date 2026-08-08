"""Run lifecycle, trace, checkpoint and report coordination."""

import sys

from moss import atomic_io
from moss.clock import now
from moss.context.prefix import skill_signature, tool_signature
from moss.memory import service as memorylib
from moss.runs import checkpoint as checkpointlib
from moss.runs import rewind as rewindlib
from moss.runs.observability import events as trace_events

DEFAULT_SHELL_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
    "COMSPEC",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "PATHEXT",
    "USERPROFILE",
    "USERNAME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "PROGRAMW6432",
)
MAX_INSTRUCTION_DOC_CHARS = 2000
STALL_EVENT_HISTORY = 40
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}


class RunCoordinator:
    def __init__(self, agent):
        self.agent = agent

    def current_runtime_identity(self):
        self = self.agent
        return checkpointlib.current_runtime_identity(self)

    def checkpoint_state(self):
        self = self.agent
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        self = self.agent
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        self = self.agent
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        self = self.agent
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        self = self.agent
        return checkpointlib.render_checkpoint_text(self)

    def rewind(self, steps=1, force=False):
        """把工作区和会话一起退回第 n 步之前（spec-07 §4.9）。"""
        self = self.agent
        return rewindlib.rewind(self, steps=steps, force=force)

    def recover_interrupted_runs(self):
        self = self.agent
        if self.depth > 0:
            return []
        return self.run_store.mark_interrupted_runs()

    def begin_run(self):
        """在一次 agent loop 开始前刷新并冻结缓存敏感注册表。"""
        self = self.agent
        if self._run_active:
            return
        self.reload_registry()
        self.refresh_prefix(force=True)
        self._run_active = True
        # 收尾摘要按 run 统计：新一轮开始就清空上一轮的改动集合与验证标记。
        self.run_changed_paths = set()
        self.run_verified = False
        self._frozen_registry = {
            "skills": dict(self.skills),
            "tools": dict(self.tools),
            "skill_signature": skill_signature(self.skills),
            "tool_signature": tool_signature(self.tools),
        }
        self._reported_registry_drifts = set()

    def end_run(self):
        self = self.agent
        self._run_active = False
        self._frozen_registry = None
        self._reported_registry_drifts = set()
        self.active_skill = None

    def start_run(self, task_state):
        self = self.agent
        run_dir = self.run_store.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        self.run_store.lease.acquire(task_state.run_id)
        return run_dir

    def heartbeat_run(self, task_state):
        self = self.agent
        return self.run_store.heartbeat(task_state)

    def release_run(self, task_state):
        self = self.agent
        return self.run_store.release_run(task_state)

    def write_task_state(self, task_state):
        self = self.agent
        payload = self.redact_artifact(task_state.to_dict())
        return self.run_store.write_task_state_payload(task_state.run_id, payload)

    def emit_progress(self, event, payload=None):
        self = self.agent
        observer = getattr(self, "progress_observer", None)
        if observer is None:
            return
        try:
            observer(event, dict(payload or {}))
        except Exception:
            pass

    def emit_trace(self, task_state, event, payload=None):
        self = self.agent
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        self.run_store.append_trace(
            task_state, payload, force_fsync=event in self.DURABLE_TRACE_EVENTS
        )
        return payload

    def note_replay_miss(self, record):
        """回放未命中：stderr 告警一次 + trace 记一条 `replay_miss`。

        为什么两处都要：stderr 是给正在盯着跑的人看的，trace 是给评测统计用的。
        只留 stderr，CI 里没人看；只留 trace，开发期会一路跑到结论才发现不对。
        """
        self = self.agent
        fingerprint = str((record or {}).get("fingerprint", ""))[:12]
        print(f"warning: replay miss for request {fingerprint}", file=sys.stderr)
        task_state = getattr(self, "current_task_state", None)
        if task_state is None:
            return None
        return self.emit_trace(task_state, trace_events.REPLAY_MISS, dict(record or {}))

    def create_checkpoint(self, task_state, user_message, trigger):
        self = self.agent
        return checkpointlib.create_checkpoint(self, task_state, user_message, trigger)

    def build_report(self, task_state):
        self = self.agent
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "model_turns": task_state.model_turns,
            "tool_calls": task_state.tool_calls,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "prompt_version": self.prefix_state.prompt_version,
            "run_manifest": {
                "prompt_version": self.prefix_state.prompt_version,
                "provider": str(getattr(self.model_client, "provider", "")),
                "model": str(getattr(self.model_client, "model", "")),
                "tool_protocol": self.resolved_tool_protocol(),
                "context_mode": self.context_mode,
            },
            "durable_promotions": list(self.last_durable_promotions),
            "durable_rejections": list(self.last_durable_rejections),
            "durable_superseded": list(self.last_durable_superseded),
            "procedural_distilled": list(self.last_procedural_distilled),
            "usage": self.last_run_budget.snapshot() if self.last_run_budget else {},
            "sandbox": self.sandbox_plan.to_dict(),
            "durability_degradations": atomic_io.degradations(),
            "snapshot_strategy": self.snapshot_strategy(),
            "truncated_bytes_lost": int(getattr(self, "truncated_bytes_lost", 0)),
            "error_signal_lost_count": int(getattr(self, "error_signal_lost_count", 0)),
            "token_calibration": self.token_calibration.to_dict(),
            "compaction_mode": self.compaction_mode,
            "compactions": list(self.session.get("compactions", [])),
            "redacted_env": self.detected_secret_env_summary(),
            "replay": self.replay_summary(),
            "model_routing": self.model_router.summary(),
            "hooks": list(self.hook_outcomes),
        }

    def summarize_run(self, task_state):
        """给交互层的一句话收尾：这一轮改了哪些文件、跑了几步、花了多少。

        为什么存在：会动用户代码的 agent 跑完必须交代清楚自己干了什么，否则
        用户只能自己 `git status` 反查。数据本来齐全地散在 report 里
        （usage、tool_steps）和 run 级累加器里（改动文件、是否验证过），
        这里聚成一个稳定的小 dict，渲染留给 CLI 层。
        """
        self = self.agent
        usage = self.last_run_budget.snapshot() if self.last_run_budget else {}
        changed = sorted(getattr(self, "run_changed_paths", set()) or set())
        return {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "tool_steps": task_state.tool_steps,
            "model_turns": task_state.model_turns,
            "changed_files": changed,
            "verified": bool(getattr(self, "run_verified", False)),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "wall_clock_s": float(usage.get("wall_clock_s", 0.0) or 0.0),
            # usd=None 表示"不知道价格"，绝不能当成 0——渲染层据此决定显不显示。
            "usd": usage.get("usd"),
            "usage_estimated": bool(usage.get("usage_estimated", False)),
        }

    def replay_summary(self):
        """这次运行是不是在回放磁带，以及偏离了多少步。非回放返回空 dict。"""
        self = self.agent
        client = self.model_client
        cassette = getattr(client, "cassette", None)
        if cassette is None or not hasattr(client, "on_miss"):
            return {}
        return {
            "cassette": str(getattr(cassette, "directory", "")),
            "on_miss": str(getattr(client, "on_miss", "")),
            "miss_count": len(getattr(client, "misses", []) or []),
        }

    def reset(self):
        self = self.agent
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(
            self.session["memory"],
            workspace_root=self.root,
            session_id=self.session["id"],
            event_callback=self.record_memory_event,
        )
        self.session_store.save(self.session)

    def resume(self, session_id):
        """把某个已保存会话的历史 + 记忆 + checkpoint 恢复进当前 agent（REPL /resume 用）。

        为什么就地改 self.session 而不是换一个新 dict：其它子系统（context/run
        coordinator 等）持有的是同一个 session dict 的引用，reset() 也是这么就地
        改的——换对象会让那些引用指向旧数据。刻意不重放有副作用的动作：交互式
        便捷恢复宁可保守，要重放走启动期的 --resume。未知 id 由 store.load 抛
        FileNotFoundError，向上透出让调用方给用户一句人话。
        """
        self = self.agent
        loaded = self.session_store.load(session_id)
        self.session.clear()
        self.session.update(loaded)
        self._ensure_session_shape()
        self.memory = memorylib.LayeredMemory(
            self.session["memory"],
            workspace_root=self.root,
            session_id=self.session["id"],
            event_callback=self.record_memory_event,
        )
        self.session_path = self.session_store.save(self.session)
        return self.session["id"]
