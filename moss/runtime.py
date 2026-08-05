"""Agent 运行时核心逻辑。

Moss 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import checkpoint as checkpointlib
from .features import memory as memorylib
from . import security as securitylib
from .context_manager import ContextManager
from .checkpoint import CHECKPOINT_NONE_STATUS
from .prompt_prefix import build_prompt_prefix, tool_signature
from .run_store import RunStore
from .session_store import SessionStore
from . import skills as skilllib
from .tool_context import ToolContext
from .tool_executor import ToolExecutor, approval_summary
from . import tools as toolkit
from .clock import now
from .token_budget import MAX_HISTORY, clip
from . import ignore as ignorelib
from . import repo_map as repo_maplib
from . import trace_events
from .workspace import (
    SnapshotResult,
    WorkspaceContext,
    capture_snapshot,
    diff_snapshots,
    find_nearest_instruction_docs,
)

DEFAULT_SHELL_ENV_ALLOWLIST = (
    # POSIX-ish
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD",
    "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER",
    # Windows: cmd.exe (通过 shell=True 启动) 没有这些变量就直接崩，
    # `run_shell` 在 Windows 上原本几乎不可用。
    "COMSPEC", "SYSTEMROOT", "SystemRoot", "WINDIR", "PATHEXT",
    "USERPROFILE", "USERNAME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "ProgramFiles", "ProgramFiles(x86)", "PROGRAMW6432",
)
# 就近指令文档进 history 的上限。它是 append-only 的事件，太长会挤掉真正的对话。
MAX_INSTRUCTION_DOC_CHARS = 2000
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}
__all__ = ["Moss", "SessionStore"]


class Moss:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.invocation_cwd = Path(getattr(workspace, "invocation_cwd", None) or workspace.cwd)
        self._ignore_rules = None
        self._last_snapshot = SnapshotResult()
        self.last_repo_map = None
        # 就近指令文档：一次会话里每份只注入一次，避免重复占预算。
        self.loaded_instruction_docs = {}
        self.pending_instruction_notices = []
        self.last_relevant_anchors = []
        self._anchor_checked = False
        # 一次运行的取消信号。中断收尾时置位，run_shell 轮询它来杀掉整个进程组。
        self.cancel_token = threading.Event()
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".moss" / "runs")
        self.interrupted_runs = self.recover_interrupted_runs()
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        # skill 先于 tool 构建：tool 注册表会根据是否存在 skill 决定要不要暴露 use_skill。
        self.skills = self.build_skills()
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)
        self.attach_repo_map(self.workspace)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        # 可选的进度观察者：CLI 用它把 agent 每一步在做什么实时打给用户看。
        # 默认 None（比如 benchmark / 子 agent 场景），完全不影响控制循环。
        self.progress_observer = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        return checkpointlib.current_runtime_identity(self)

    def checkpoint_state(self):
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpointlib.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return toolkit.build_tool_registry(self.tool_context())

    def native_tool_definitions(self):
        native_tool_format = str(getattr(self.model_client, "native_tool_format", "") or "")
        if not native_tool_format:
            return None
        return toolkit.native_tool_definitions(self.tools, native_tool_format)

    def recover_interrupted_runs(self):
        # 启动恢复只属于顶层 agent。delegate 子 agent 与父 agent 共用同一个
        # run_store，且是在父 agent 运行途中（父 run 的 task_state 正处于
        # running）被构造的——如果这里对共享 run_store 做扫描，会把父 agent
        # 正在进行的 run 误判成 interrupted 并覆写它的 report。所以 depth>0
        # 一律跳过恢复。
        if self.depth > 0:
            return []
        return self.run_store.mark_interrupted_runs()

    def build_skills(self):
        return skilllib.build_skill_registry(self.root)

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names()
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(self.allowed_tools)
        return {
            name: tool
            for name, tool in tools.items()
            if name in allowed
        }

    def tool_signature(self):
        return tool_signature(self.tools)

    def build_prefix(self):
        return build_prompt_prefix(workspace=self.workspace, tools=self.tools, skills=self.skills)

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 用 invocation_cwd 而不是 repo_root 刷新：在子目录启动的会话里，
        # 传 repo_root 会让第二轮起 cwd 悄悄退化成仓库根，
        # 工作区身份和指纹跟着一起变，看起来像“工作区被改过”。
        refreshed_workspace = WorkspaceContext.build(self.invocation_cwd, repo_root_override=self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed or force:
            self.attach_repo_map(refreshed_workspace)
            self.workspace = refreshed_workspace

        # 重新发现 skill、重建 tool 注册表：这样磁盘上新增/删除的 skill
        # （以及 use_skill 是否该暴露）都能在本轮被反映出来。
        self.skills = self.build_skills()
        self.tools = self._apply_tool_allowlist(self.build_tools())

        # prefix 的重建判据是整段 prefix 的 hash，而不是 workspace 指纹：
        # 组 prefix 只是字符串拼接 + 一次 sha256，开销极小，所以每轮都重建，
        # 由 hash 决定它到底有没有变（workspace / tools / skills 任一变化都会被捕获）。
        prefix_state = self.build_prefix()
        prefix_changed = force or previous_hash is None or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()

    def history_text(self):
        # 单一口径：直接复用 ContextManager 的历史渲染，避免出现两套
        # 略有差异的压缩逻辑（曾经这里有一份独立实现，会和真正进 prompt 的
        # 历史对不上，metadata 里的 history_chars 也因此失真）。
        return clip(self.context_manager.render_history_text(), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        # session 会进入下一轮 prompt，也会长期落盘；这里必须和 trace/report
        # 一样先过脱敏边界，避免一次工具输出把 secret 带进可恢复上下文。
        safe_item = self.redact_artifact(dict(item or {}))
        self.session["history"].append(safe_item)
        self.session_path = self.session_store.save(self.session)

    def start_run(self, task_state):
        run_dir = self.run_store.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        # task_state 同样是落盘审计工件，不能绕过 runtime 的 secret 策略。
        payload = self.redact_artifact(task_state.to_dict())
        return self.run_store.write_task_state_payload(task_state.run_id, payload)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(secret_env_names=self.secret_env_names)

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(secret_env_names=self.secret_env_names)

    def secret_env_summary(self):
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(secret_env_names=self.secret_env_names)

    def redact_text(self, text):
        return securitylib.redact_text(text, secret_env_names=self.secret_env_names)

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(value, key=key, secret_env_names=self.secret_env_names)

    def shell_env(self):
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "skill_count": len(self.skills),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prefix_stable_hash": self.prefix_state.stable_hash,
                # 缓存路由键用稳定头的 hash，而不是整段 prefix 的 hash：
                # 这样 agent 自己改文件导致的 workspace 抖动不会让缓存键每轮失效。
                "prompt_cache_key": self.prefix_state.stable_hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "skill_signature": self.prefix_state.skill_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def emit_progress(self, event, payload=None):
        # 把「agent 现在在做什么」推给可选的观察者。observer 只负责展示，
        # 不能影响控制流，所以这里吞掉它抛出的任何异常。
        observer = getattr(self, "progress_observer", None)
        if observer is None:
            return
        try:
            observer(event, dict(payload or {}))
        except Exception:
            pass

    def emit_trace(self, task_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(task_state, payload)
        return payload

    def attach_repo_map(self, workspace):
        """把仓库地图挂到 workspace 段。

        为什么在这里而不是 WorkspaceContext.build 里：地图要用 agent 的忽略口径
        和 .moss/cache 目录，而 workspace 构建是个不认识 agent 的纯函数。
        MOSS_REPO_MAP=off 是一键回退开关——关掉后 prefix 与加地图前逐字节一致。
        """
        if str(os.environ.get("MOSS_REPO_MAP", "on")).strip().lower() in {"off", "0", "false", "no"}:
            workspace.repo_map_text = ""
            self.last_repo_map = None
            return workspace
        try:
            budget = int(os.environ.get("MOSS_REPO_MAP_BUDGET", repo_maplib.DEFAULT_BUDGET_TOKENS))
        except ValueError:
            budget = repo_maplib.DEFAULT_BUDGET_TOKENS
        try:
            repo_map = repo_maplib.get_repo_map(
                self.root, ignore=self.workspace_ignore_rules(), budget_tokens=budget
            )
            workspace.repo_map_text = repo_maplib.render_repo_map(repo_map, budget)
            self.last_repo_map = repo_map
        except Exception:
            # 地图是纯增益特性，构建失败绝不能挡住 agent 起来干活。
            workspace.repo_map_text = ""
            self.last_repo_map = None
        return workspace

    def relevant_file_anchors(self, user_message, limit=5):
        """给当前请求算一组“最可能相关的文件”，作为模型的起点锚。

        进的是 relevant_memory 段而不是 prefix：它随每轮请求变化，
        放进稳定段会让 prompt 缓存每轮失效。
        """
        if self.last_repo_map is None:
            self.last_relevant_anchors = []
            return []
        try:
            anchors = repo_maplib.rank_relevant_files(self.last_repo_map, user_message, limit=limit)
        except Exception:
            anchors = []
        self.last_relevant_anchors = anchors
        return anchors

    def note_anchor_outcome(self, path):
        """记录起点锚有没有命中：模型第一次真正读的文件在不在候选里。

        为什么只看第一次：锚的价值就是省掉“认路”那几步，模型读完第一个文件之后
        它已经自己会走了，后面读什么不能算在锚头上。
        anchor_miss 率超过 50% 就该把这个特性关掉（spec-01 §7）。
        """
        if self._anchor_checked or not self.last_relevant_anchors:
            return None
        self._anchor_checked = True
        path = str(path or "").replace("\\", "/").lstrip("./")
        if path in self.last_relevant_anchors:
            return None
        return {"path": path, "anchors": list(self.last_relevant_anchors)}

    def note_nearby_instructions(self, path):
        """文件类工具碰到某个目录后，把该目录祖先链上的就近指令文档排队注入。

        为什么是“碰到之后”而不是一开始就全塞进 prefix：子目录里的 AGENTS.md
        只有在真的要动那块代码时才有意义，全量注入等于让每个任务都为所有子目录
        的规则付 token。同一份文档在一次会话里只注一次（append-only，见 spec-04）。
        """
        try:
            found = find_nearest_instruction_docs(self.root, self.root / str(path))
        except Exception:
            return []
        if not found:
            return []
        nearest = found[0]
        if len(found) > 1:
            # 同名规则冲突：最近目录优先。记一笔，否则"为什么这条规则没生效"
            # 只能靠人肉比对目录层级。
            self.pending_instruction_notices.append(
                {
                    "event": trace_events.INSTRUCTION_CONFLICT,
                    "path": nearest,
                    "winner": nearest,
                    "shadowed": found[1:],
                }
            )
        if nearest in self.loaded_instruction_docs:
            return []
        try:
            text = (self.root / nearest).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        scope = str(Path(nearest).parent.as_posix())
        scope = "." if scope in {"", "."} else scope
        self.loaded_instruction_docs[nearest] = scope
        notice = {
            "event": trace_events.INSTRUCTION_LOADED,
            "path": nearest,
            "scope": scope,
            "content": clip(text, MAX_INSTRUCTION_DOC_CHARS),
        }
        self.pending_instruction_notices.append(notice)
        return [notice]

    def drain_instruction_notices(self):
        notices = list(self.pending_instruction_notices)
        self.pending_instruction_notices.clear()
        return notices

    def workspace_ignore_rules(self):
        """快照/地图共用的一套忽略口径，按 root 缓存。

        每次 risky 工具都重新解析 .gitignore 是纯浪费；而两处口径不一致会让
        模型看到的目录树和 diff 里的路径对不上。
        """
        if self._ignore_rules is None:
            self._ignore_rules = ignorelib.IgnoreRules.load(
                self.root,
                extra_patterns=ignorelib.parse_exclude_globs(os.environ.get("MOSS_SNAPSHOT_EXCLUDE", "")),
            )
        return self._ignore_rules

    def capture_workspace_snapshot(self):
        # 实现下沉到 workspace.py：快照语义属于工作区，不属于控制循环。
        # 增量策略下 key 集合只覆盖变更集，所以要把上一张快照的 key 一起带上，
        # 否则“文件被删掉”会因为两边都没有这个 key 而漏报。
        result = capture_snapshot(
            self.root,
            ignore=self.workspace_ignore_rules(),
            strategy=os.environ.get("MOSS_SNAPSHOT_STRATEGY", "auto") or "auto",
            git_changed=tuple(self._last_snapshot.entries),
            detailed=True,
        )
        self._last_snapshot = result
        return result.entries

    def diff_workspace_snapshots(self, before, after):
        return diff_snapshots(before, after, untracked=self._last_snapshot.untracked)

    def snapshot_strategy(self):
        """最近一次快照实际走的策略，进 report 供评测口径识别降级情况。"""
        return self._last_snapshot.strategy

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpointlib.create_checkpoint(self, task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        return memorylib.update_memory_after_tool(self, name, args, result)

    def record_process_note_for_tool(self, name, metadata):
        return memorylib.record_process_note_for_tool(self, name, metadata)

    def promote_durable_memory(self, user_message, final_answer):
        promotions, rejections = memorylib.extract_durable_promotions(
            user_message, final_answer, redact_text=self.redact_text
        )
        promoted, superseded = self.memory.promote_durable(promotions)
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = promoted
        self.last_durable_rejections = rejections
        self.last_durable_superseded = superseded
        return promoted, rejections, superseded

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    def execute_tool(self, name, args):
        result = self.tool_executor.execute(name, args)
        self._last_tool_result_metadata = dict(result.metadata)
        return result

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是“模型会不会想调用工具”，而是
        “平台有没有在执行前把边界守住”。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的“模型决定要调用工具”之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        return self.execute_tool(name, args).content

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 这里提前挡掉最简单的这种循环。
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "durable_promotions": list(self.last_durable_promotions),
            "durable_rejections": list(self.last_durable_rejections),
            "durable_superseded": list(self.last_durable_superseded),
            "redacted_env": self.detected_secret_env_summary(),
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self.tool_context(), name, args)

    def tool_context(self):
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_delegate=self.spawn_delegate,
            skills_provider=lambda: self.skills,
            cancel_token=self.cancel_token,
        )

    def delegate_session_store(self):
        # 委派子 agent 会创建一个一次性的只读会话。如果它和用户会话写进同一个
        # sessions 目录，`--resume latest`（按 mtime 取最新）就会恢复到这个刚写完
        # 的临时委派会话，而不是用户自己的工作会话。把它们隔离到独立目录，既避免
        # 污染 latest()，又保留可审计的委派轨迹。
        return SessionStore(str(self.root / ".moss" / "delegates"))

    def spawn_delegate(self, args):
        task = str(args.get("task", "")).strip()
        child = Moss(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.delegate_session_store(),
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
        return "delegate_result:\n" + child.ask(task)

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self.tool_context(), args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self.tool_context(), args)

    def tool_search_text(self, args):
        return toolkit.tool_search_text(self.tool_context(), args)

    def tool_run_shell(self, args):
        result = toolkit.tool_run_shell(self.tool_context(), args)
        return result.content if hasattr(result, "content") else result

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self.tool_context(), args)

    def tool_edit_file(self, args):
        return toolkit.tool_edit_file(self.tool_context(), args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self.tool_context(), args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {approval_summary(self, name, args)}? [y/N] ")
        except (EOFError, UnicodeDecodeError):
            # 读不到（或读到半个 UTF-8 序列）一律按"没批准"处理：
            # 审批是安全护栏，读不清的回答绝不能默认放行。
            return False
        return answer.strip().lower() in {"y", "yes"}

    def reset(self):
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved
