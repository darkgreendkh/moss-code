"""Agent 运行时核心逻辑。

Moss 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import os
import sys
import threading
import warnings
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import checkpoint as checkpointlib
from . import compaction as compactionlib
from .features import memory as memorylib
from .features.memory_records import SourceRef
from . import security as securitylib
from .context_manager import ContextManager
from .model_request import PromptBundle
from .checkpoint import CHECKPOINT_NONE_STATUS
from .prompt_prefix import build_prompt_prefix, skill_signature, tool_signature
from .run_store import RunStore
from .session_store import SessionStore
from . import skills as skilllib
from .tool_context import ToolContext
from .tool_executor import ToolExecutor, approval_summary
from . import tools as toolkit
from .clock import now
from .token_budget import (
    MAX_HISTORY,
    TokenCalibrationStore,
    calibrated_measure,
    clip,
    estimate_tokens,
    exact_token_counter,
)
from . import ignore as ignorelib
from . import repo_map as repo_maplib
from . import budget as budgetlib
from . import policy as policylib
from . import sandbox as sandboxlib
from . import stall as stalllib
from .verification import is_verification_command
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
# 留给停滞检测的工具结果窗口。比检测窗口大一些，no_progress 要看"更早读过哪些路径"。
STALL_EVENT_HISTORY = 40
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
        parallel_tools=False,
        run_budget_limits=None,
        verify_before_final=True,
        injection_scan=True,
        policy=None,
        sandbox="auto",
        allowed_network_hosts=None,
        tool_protocol="auto",
        context_mode="rerender",
        reflect_mode="rule",
        compaction_mode="off",
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
        # 只读工具批内并发。默认关闭：先灰度，评测证明收益后再翻默认值。
        self.parallel_tools = bool(parallel_tools)
        # 收尾前自检：改了文件却没跑过验证时，拦一次并提示先验证。
        self.verify_before_final = bool(verify_before_final)
        # 注入扫描。命中后本 run 剩余的 risky 工具强制走审批。
        self.injection_scan = bool(injection_scan)
        self.injection_findings = []
        # L1 沙箱：网络类命令即使 --approval auto 也要问一次；
        # 给了白名单就只放行名单内的域名。
        self.allowed_network_hosts = tuple(allowed_network_hosts or ())
        if tool_protocol not in {"auto", "native", "text"}:
            raise ValueError("tool_protocol must be auto, native, or text")
        self.tool_protocol = tool_protocol
        if context_mode not in {"rerender", "append_only"}:
            raise ValueError("context_mode must be rerender or append_only")
        self.context_mode = context_mode
        if reflect_mode not in {"off", "rule", "model"}:
            raise ValueError("reflect_mode must be off, rule, or model")
        self.reflect_mode = reflect_mode
        if compaction_mode not in {"off", "rule", "model"}:
            raise ValueError("compaction_mode must be off, rule, or model")
        # 默认 off = 今天的纯截断行为，也是消融基线（spec-06 §5）。
        # 等评测证明收益之后再翻默认值。
        self.compaction_mode = compaction_mode
        # 历史段连续几轮触发了收缩。连续两轮说明不是偶发的大输出，
        # 而是历史本身已经装不下了 —— 那是该压缩而不是继续削的信号。
        self.history_reduction_streak = 0
        self.sandbox_plan = sandboxlib.announce(sandboxlib.detect(sandbox))
        # 审批决定的记忆：{(工具, 风险, 路径桶): 是否允许}。刻意只存在内存里，
        # 会话结束即失效——落盘的"上次批过"会变成永久后门。
        self._approval_memory = {}
        # 能力/路径策略。read_only 也归它管，这样"只读"不再是散落在多处的特判。
        self.policy = policy if policy is not None else policylib.Policy.build(read_only=read_only)
        # 多维预算的上限（步数之外还有 token / 时间 / 金额）。默认全 None，
        # 行为与加预算前完全一致。
        self.run_budget_limits = dict(run_budget_limits or {})
        self.last_run_budget = None
        # 当前计划（update_plan 写入）。它同时进 task_state 和 prompt 尾部。
        self.current_plan = []
        self._plan_step_started_at = 0
        self._plan_pressure_reported = set()
        # 停滞检测用的工具结果窗口（workspace_changed / tool_error_code 不在 history 里）。
        self._tool_outcomes = []
        # 同一类停滞只干预一次：重复喊同一句话既费 token 又没有新信息。
        self.stall_notices_sent = set()
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
            session_id=self.session["id"],
            event_callback=self.record_memory_event,
        )
        self.session["memory"] = self.memory.to_dict()
        # skill 先于 tool 构建：tool 注册表会根据是否存在 skill 决定要不要暴露 use_skill。
        self.skills = self.build_skills()
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)
        self.attach_repo_map(self.workspace)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.token_calibration_store = TokenCalibrationStore(
            self.root / ".moss" / "cache" / "token_calibration.json"
        )
        self.token_calibration = self.token_calibration_store.calibration(
            getattr(model_client, "provider", ""), getattr(model_client, "model", "")
        )
        self.context_manager = ContextManager(self, measure=self.token_measure())
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        # 卸载 artifact 的序号。只读工具批可以并发执行，序号必须自己加锁，
        # 否则同一批里两个大输出会抢到同一个文件名。
        self._artifact_seq = 0
        self._artifact_lock = threading.Lock()
        # 本次运行因硬截断而永久丢掉的字节数。卸载生效后它应当恒为 0。
        self.truncated_bytes_lost = 0
        # 压缩把"说明成败的行"全切掉了的次数。压缩器最不该犯的错，
        # 做成可统计的标签而不是靠人工抽检。
        self.error_signal_lost_count = 0
        # 可选的进度观察者：CLI 用它把 agent 每一步在做什么实时打给用户看。
        # 默认 None（比如 benchmark / 子 agent 场景），完全不影响控制循环。
        self.progress_observer = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self.last_procedural_distilled = []
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }
        # run 内冻结会影响稳定头的注册表。磁盘漂移只记录，不在半途中换工具，
        # 否则 provider 看到的 prompt_cache_key 会在同一任务里突然变化。
        self._run_active = False
        self._frozen_registry = None
        self._reported_registry_drifts = set()

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

    def resolved_tool_protocol(self):
        if self.tool_protocol != "auto":
            return self.tool_protocol
        capabilities = getattr(self.model_client, "capabilities", None)
        supports_native = bool(
            (capabilities.supports_native_tools if capabilities is not None else False)
            or getattr(self.model_client, "supports_native_tools", False)
        )
        return "native" if supports_native else "text"

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
        return build_prompt_prefix(
            workspace=self.workspace,
            tools=self.tools,
            skills=self.skills,
            protocol=self.resolved_tool_protocol(),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def reload_registry(self):
        """立即重读 skill/tool 注册表；只允许在模型 run 之外调用。"""
        if self._run_active:
            raise RuntimeError("cannot reload tools or skills during an active run")
        previous_skills = set(self.skills)
        previous_tools = set(self.tools)
        self.skills = self.build_skills()
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self._apply_prefix_state(self.build_prefix())
        return {
            "added": sorted(set(self.skills) - previous_skills),
            "removed": sorted(previous_skills - set(self.skills)),
            "tools_added": sorted(set(self.tools) - previous_tools),
            "tools_removed": sorted(previous_tools - set(self.tools)),
        }

    def begin_run(self):
        """在一次 agent loop 开始前刷新并冻结缓存敏感注册表。"""
        if self._run_active:
            return
        self.reload_registry()
        # 两次 run 之间可以发生外部修改；在冻结前抓一次最新 workspace。
        self.refresh_prefix(force=True)
        self._run_active = True
        self._frozen_registry = {
            "skills": dict(self.skills),
            "tools": dict(self.tools),
            "skill_signature": skill_signature(self.skills),
            "tool_signature": tool_signature(self.tools),
        }
        self._reported_registry_drifts = set()

    def end_run(self):
        self._run_active = False
        self._frozen_registry = None
        self._reported_registry_drifts = set()

    def _detect_registry_drift(self):
        if not self._run_active or not self._frozen_registry:
            return None
        live_skills = self.build_skills()
        live_skill_signature = skill_signature(live_skills)
        if live_skill_signature == self._frozen_registry["skill_signature"]:
            return None
        frozen_names = set(self._frozen_registry["skills"])
        live_names = set(live_skills)
        changed = sorted(
            name
            for name in frozen_names & live_names
            if self._frozen_registry["skills"][name] != live_skills[name]
        )
        return {
            "added": sorted(live_names - frozen_names),
            "removed": sorted(frozen_names - live_names),
            "changed": changed,
        }

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 用 invocation_cwd 而不是 repo_root 刷新：在子目录启动的会话里，
        # 传 repo_root 会让第二轮起 cwd 悄悄退化成仓库根，
        # 工作区身份和指纹跟着一起变，看起来像“工作区被改过”。
        refreshed_workspace = WorkspaceContext.build(self.invocation_cwd, repo_root_override=self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if (workspace_changed or force) and not self._run_active:
            self.attach_repo_map(refreshed_workspace)
            self.workspace = refreshed_workspace

        registry_drift = self._detect_registry_drift()
        if not self._run_active:
            # REPL 空闲期与显式 prompt_metadata 调用仍保持原行为：磁盘改动立即生效。
            self.skills = self.build_skills()
            self.tools = self._apply_tool_allowlist(self.build_tools())
        elif registry_drift:
            drift_key = (
                tuple(registry_drift["added"]),
                tuple(registry_drift["removed"]),
                tuple(registry_drift["changed"]),
            )
            if drift_key not in self._reported_registry_drifts:
                self._reported_registry_drifts.add(drift_key)
                if self.current_task_state is not None:
                    self.emit_trace(self.current_task_state, trace_events.TOOL_REGISTRY_DRIFT, registry_drift)

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
            "registry_drift": bool(registry_drift),
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

    def token_measure(self):
        """这一轮预算用的计量函数：真值优先，其次校准过的估算。

        探测到 tiktoken 就直接用真值（ratio 强制 1.0）；否则用
        `estimate_tokens * ratio`，ratio 来自最近 50 条"我估了多少 / 后端说是多少"。
        """
        exact = exact_token_counter(getattr(self.model_client, "model", ""))
        if exact is not None:
            self.token_calibration = replace(self.token_calibration, ratio=1.0, exact=True)
            return exact
        return calibrated_measure(estimate_tokens, self.token_calibration)

    def record_token_usage_sample(self, estimated_tokens, actual_tokens):
        """记一条 (估算, 后端真值) 样本，并把校准结果用到下一轮预算上。

        只在后端**真的报了** usage 时才记：拿我们自己的估算当"真值"去校准
        我们自己的估算，只会把偏差固化下来。
        """
        if self.token_calibration.exact:
            return self.token_calibration
        self.token_calibration = self.token_calibration_store.record(
            getattr(self.model_client, "provider", ""),
            getattr(self.model_client, "model", ""),
            estimated_tokens,
            actual_tokens,
        )
        self.context_manager.measure = self.token_measure()
        return self.token_calibration

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def clear_pending_history(self):
        """去掉历史里的 pending 标记。

        pending 的含义是"这条内容已经在本轮 prompt 的别处出现过了"，
        只对当前这一次运行成立；运行结束后它就是普通历史。
        """
        changed = False
        for item in self.session.get("history", []):
            if item.pop("pending", None):
                changed = True
        if changed:
            self.session_path = self.session_store.save(self.session)

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
        bundle = self._build_prompt_bundle_and_metadata(user_message)
        return bundle.text, bundle.metadata

    def _build_prompt_bundle_and_metadata(self, user_message):
        result = self.build_context_result(user_message)
        return PromptBundle(request=result.request, text=result.text, metadata=result.metadata)

    def build_context_result(self, user_message):
        """组这一轮的 prompt，并带上 admission 判定（能不能发）。"""
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        bundle = self.context_manager.build_result(user_message)
        metadata = dict(bundle.metadata)
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
                "prompt_version": self.prefix_state.prompt_version,
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
        return replace(bundle, metadata=metadata)

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

    def record_memory_event(self, event, payload=None):
        task_state = getattr(self, "current_task_state", None)
        if task_state is None:
            return None
        return self.emit_trace(task_state, event, payload)

    def memory_source_refs(self):
        task_state = getattr(self, "current_task_state", None)
        run_id = task_state.run_id if task_state is not None else f"session:{self.session['id']}"
        return (SourceRef(run_id=run_id),)

    @staticmethod
    def _memory_rejection(reason):
        return {"status": "rejected", "reason": str(reason)}

    def memory_write_action(self, args):
        record, reason = self.memory.write_durable(
            scope=args.get("scope", ""),
            topic=args.get("topic", ""),
            text=args.get("text", ""),
            tags=args.get("tags", ()),
            trust="model",
            source_refs=self.memory_source_refs(),
        )
        if record is None:
            return self._memory_rejection(reason)
        self._save_memory_state()
        return {"status": "written", "id": record.id, "scope": record.scope, "trust": record.trust}

    def memory_update_action(self, args):
        record, reason = self.memory.update_durable(
            args.get("id", ""),
            args.get("text", ""),
            trust="model",
            source_refs=self.memory_source_refs(),
        )
        if record is None:
            return self._memory_rejection(reason)
        self._save_memory_state()
        return {"status": "updated", "id": record.id, "supersedes": list(record.supersedes)}

    def memory_delete_action(self, args):
        record, reason = self.memory.delete_durable(args.get("id", ""))
        if record is None:
            return self._memory_rejection(reason)
        self._save_memory_state()
        return {"status": "deleted", "deleted": record.id}

    def memory_search_action(self, args):
        return self.memory.search(args.get("query", ""), limit=int(args.get("limit", 5)))

    def _save_memory_state(self):
        self.session["memory"] = self.memory.to_dict()
        self.session_path = self.session_store.save(self.session)

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
        self.memory.mark_used(final_answer)
        promotions, rejections = memorylib.extract_durable_promotions(
            user_message, final_answer, redact_text=self.redact_text
        )
        task_state = getattr(self, "current_task_state", None)
        source_refs = ()
        if task_state is not None:
            source_refs = (SourceRef(run_id=task_state.run_id),)
        promoted, superseded = self.memory.promote_durable(
            promotions, source_refs=source_refs, trust="user"
        )
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = promoted
        self.last_durable_rejections = rejections
        self.last_durable_superseded = superseded
        self.last_procedural_distilled = self.distill_current_run()
        return promoted, rejections, superseded

    def distill_current_run(self):
        if self.reflect_mode == "off" or self.current_task_state is None:
            return []
        records = memorylib.distill_run(
            self.run_store.read_trace(self.current_task_state.run_id),
            mode=self.reflect_mode,
            workspace_root=self.root,
        )
        stored = [self.memory.durable_store.store.append_procedural(record) for record in records]
        self.session["memory"] = self.memory.to_dict()
        return [record.id for record in stored]

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    def execute_tool(self, name, args, defer_side_effects=False):
        result = self.tool_executor.execute(name, args, defer_side_effects=defer_side_effects)
        self._last_tool_result_metadata = dict(result.metadata)
        return result

    def execute(self, request):
        """唯一的结构化执行入口，供 MCP server / hooks / 评测使用。

        为什么需要它：把 `tool_*` 收成私有之后，外部集成必须有一个受护栏的入口，
        否则大家会退回去直接调 toolkit——那正是这次要堵的口子。
        request 可以是 ActionRequest，也可以是 {"name":..., "args":...} 字典。
        """
        name = getattr(request, "name", None)
        args = getattr(request, "args", None)
        if name is None and isinstance(request, dict):
            name = request.get("name")
            args = request.get("args")
        return self.execute_tool(str(name or ""), dict(args or {}))

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
        # 这里提前挡掉最简单的这种循环；更复杂的环（A→B→A、错误风暴、
        # 只读不改）交给 stall.detect_stall 在每轮结束后统一判定。
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        return stalllib.is_repeated_call(tool_events, name, args)

    def set_plan(self, plan):
        """把模型给的计划写进 task_state，并落 trace。

        计划是"显式的意图声明"：跑偏时能对照它看出偏在哪一步，
        而不是只能靠步数上限兜底。plan_drift 的判定在 spec-08 离线做，
        主循环只负责把它记下来。
        """
        self.current_plan = list(plan or [])
        task_state = self.current_task_state
        if task_state is not None:
            task_state.plan = list(self.current_plan)
            self.emit_trace(task_state, trace_events.PLAN_UPDATED, {"steps": list(self.current_plan)})
        # 换了计划就重新开始算某一步的消耗，否则新计划一上来就被判压力过大。
        self._plan_step_started_at = self.current_task_state.tool_steps if task_state else 0
        self._plan_pressure_reported = set()
        return self.current_plan

    def render_plan_text(self):
        return toolkit.render_plan(self.current_plan)

    def check_plan_pressure(self):
        """某个 in_progress 步骤吃掉的步数远超均摊预算时，建议重规划。

        软预算而不是硬拦截：计划本来就会变，真正的问题是"卡在同一步却不承认"。
        """
        plan = self.current_plan
        task_state = self.current_task_state
        if not plan or task_state is None:
            return None
        current = next((step for step in plan if step["status"] == "in_progress"), None)
        if current is None or current["id"] in self._plan_pressure_reported:
            return None
        allowance = max(1, self.max_steps // max(1, len(plan))) * 2
        spent = task_state.tool_steps - self._plan_step_started_at
        if spent < allowance:
            return None
        self._plan_pressure_reported.add(current["id"])
        return {"step_id": current["id"], "title": current["title"], "spent_steps": spent}

    def new_run_budget(self):
        """给一次运行开一份预算账本。"""
        limits = dict(self.run_budget_limits)
        return budgetlib.RunBudget(max_steps=0, **limits)

    def price_for_usage(self, input_tokens, output_tokens):
        """把 token 数换算成金额；查不到价格返回 None。

        返回 None 而不是 0：把未知当成 0 会让金额上限永远不触发，
        正好在最该拦的时候不拦。价目表在 spec-08 的 evaluation/pricing.py 落地，
        这里先留出接入点。
        """
        pricing = getattr(self.model_client, "pricing", None)
        if not pricing:
            return None
        try:
            return (
                float(input_tokens) * float(pricing["input_usd_per_1k"]) / 1000.0
                + float(output_tokens) * float(pricing["output_usd_per_1k"]) / 1000.0
            )
        except (KeyError, TypeError, ValueError):
            return None

    def stall_events(self):
        """给停滞检测用的最近工具事件序列。

        从 history 取 name/args，从每次执行的 metadata 取 workspace_changed
        和 tool_error_code——后两者不在 history 里，所以单独攒一份。
        """
        return list(self._tool_outcomes)

    def record_tool_outcome(self, name, args, metadata):
        self._tool_outcomes.append(
            {
                "name": name,
                "args": args,
                "workspace_changed": bool((metadata or {}).get("workspace_changed")),
                "tool_error_code": str((metadata or {}).get("tool_error_code", "") or ""),
                "verification": is_verification_command(name, args, metadata),
            }
        )
        del self._tool_outcomes[: -STALL_EVENT_HISTORY]

    def detect_stall(self):
        return stalllib.detect_stall(self.stall_events())

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
            # model_turns / tool_calls 是成本与失败率的口径（spec-02 §4.2）。
            # tool_steps 的语义不动，这两个只加不改，历史指标仍然可比。
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
            # 沙箱状态进 report：降级必须看得见，评测口径也要能区分。
            "sandbox": self.sandbox_plan.to_dict(),
            "snapshot_strategy": self.snapshot_strategy(),
            # 卸载生效后这个数应当恒为 0：大输出全部落盘，没有字节被永久丢掉。
            "truncated_bytes_lost": int(getattr(self, "truncated_bytes_lost", 0)),
            "error_signal_lost_count": int(getattr(self, "error_signal_lost_count", 0)),
            "token_calibration": self.token_calibration.to_dict(),
            "compaction_mode": self.compaction_mode,
            "compactions": list(self.session.get("compactions", [])),
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
            plan_writer=self.set_plan,
            memory_writer=self.memory_write_action,
            memory_updater=self.memory_update_action,
            memory_deleter=self.memory_delete_action,
            memory_searcher=self.memory_search_action,
            sandbox_plan=self.sandbox_plan,
            run_path_resolver=self.run_path,
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
            reflect_mode="off",
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
        return "delegate_result:\n" + child.ask(task)

    # 以下是**未经护栏**的原始工具调用。它们必须保持私有：
    # 公开出去就等于在 ToolExecutor（allowlist -> 校验 -> deny -> 重复检测 ->
    # 审批 -> 快照 diff）旁边开了一条直通车，read_only / approval / allowed_tools
    # 全部形同虚设。唯一公开的执行入口是 run_tool / execute。
    def _tool_list_files(self, args):
        return toolkit.tool_list_files(self.tool_context(), args)

    def _tool_read_file(self, args):
        return toolkit.tool_read_file(self.tool_context(), args)

    def _tool_search_text(self, args):
        return toolkit.tool_search_text(self.tool_context(), args)

    def _tool_run_shell(self, args):
        result = toolkit.tool_run_shell(self.tool_context(), args)
        return result.content if hasattr(result, "content") else result

    def _tool_write_file(self, args):
        return toolkit.tool_write_file(self.tool_context(), args)

    def _tool_edit_file(self, args):
        return toolkit.tool_edit_file(self.tool_context(), args)

    def _tool_delegate(self, args):
        return toolkit.tool_delegate(self.tool_context(), args)

    def __getattr__(self, name):
        """老的公共 `tool_*` 方法：保留一个发 DeprecationWarning 的兼容层。

        它们过去能整体绕过 ToolExecutor，这是本次收口要堵的口子。
        兼容层不再直连 toolkit，而是转发到 run_tool——也就是说，
        行为从"无护栏"变成"受护栏约束"，这是刻意的破坏性变更。
        """
        if not name.startswith("tool_"):
            raise AttributeError(name)
        tool_name = name[len("tool_") :]
        if tool_name not in toolkit.legal_tool_names():
            raise AttributeError(name)

        def deprecated_runner(args):
            warnings.warn(
                f"Moss.{name}() is deprecated and now runs through the tool executor; use run_tool({tool_name!r}, args).",
                DeprecationWarning,
                stacklevel=2,
            )
            return self.run_tool(tool_name, args)

        return deprecated_runner

    def _is_network_command(self, name, args):
        if name != "run_shell":
            return False
        return toolkit.classify_shell_command((args or {}).get("command", "")).level == "network"

    def flag_injection_suspected(self, finding):
        """记下一次注入嫌疑，并让本 run 剩余的 risky 工具强制走审批。

        为什么是"强制审批"而不是"拒绝"：检测必然有误报，拒绝会把正常任务
        直接打断；而强制审批只是把决定权交回给人，代价是一次确认。
        """
        self.injection_findings.append(finding)
        return finding

    @property
    def injection_suspected(self):
        return bool(self.injection_findings)

    def network_hosts_refused(self, name, args):
        """给了网络白名单时，命令里出现的域名必须在名单内。"""
        if name != "run_shell" or not self.allowed_network_hosts:
            return ()
        hosts = toolkit.shell_policy.extract_hosts((args or {}).get("command", ""))
        return tuple(
            host for host in hosts if not toolkit.shell_policy.host_allowed(host, self.allowed_network_hosts)
        )

    def approve(self, name, args):
        if self.read_only:
            return False
        refused = self.network_hosts_refused(name, args)
        if refused:
            return False
        if self.approval_policy == "auto" and self._is_network_command(name, args):
            # 网络类命令即使在 --approval auto 下也要问一次：它把数据送出去，
            # 是唯一一类"做错了收不回来"的操作。
            return self._ask_for_approval(name, args)
        if self.approval_policy == "auto" and self.injection_suspected:
            # 本 run 里出现过注入嫌疑：即使 --approval auto 也要问一次。
            return self._ask_for_approval(name, args)
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        return self._ask_for_approval(name, args)

    def approval_class(self, name, args):
        """"本类"的定义：(工具, 风险等级, 路径桶)；shell 额外按 argv[0] 归类。

        粒度太粗（只按工具名）会让"允许一次 git status"变成"允许所有 shell"；
        太细（按完整参数）则等于没有记忆，用户还是每次都要按 y。
        """
        args = args or {}
        risk = "high"
        bucket = ""
        if name == "run_shell":
            classified = toolkit.classify_shell_command(args.get("command", ""))
            risk = classified.level
            bucket = classified.argvs[0][0] if classified.argvs and classified.argvs[0] else ""
        else:
            tool = self.tools.get(name) or {}
            risk = "high" if tool.get("risky") else "low"
            raw_path = str(args.get("path", "")).strip()
            if raw_path:
                # 按顶层目录分桶：改 src/ 和改 tests/ 是两类决定。
                bucket = raw_path.replace("\\", "/").lstrip("./").split("/", 1)[0]
        return (name, risk, bucket)

    def _read_approval_answer(self, question):
        """从 tty 读审批回答。

        为什么不用 input()：一旦有人 `echo task | moss`，stdin 就是管道，
        input() 会把**任务文本**当成审批回答读走——用户的第一行输入变成了 "y"。
        所以优先打开 /dev/tty；打不开就降级为拒绝，保持"读不清 = 不批准"。
        """
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write(question)
                tty.flush()
                return (tty.readline() or "").strip().lower()
        except (OSError, UnicodeDecodeError):
            pass
        # Windows 没有 /dev/tty：退回 input()，但先确认 stdin 真的是终端。
        if not sys.stdin or not sys.stdin.isatty():
            return ""
        try:
            return input(question).strip().lower()
        except (EOFError, UnicodeDecodeError):
            return ""

    def _ask_for_approval(self, name, args):
        approval_class = self.approval_class(name, args)
        remembered = self._approval_memory.get(approval_class)
        if remembered is not None:
            return remembered
        prefix = "[injection suspected] " if self.injection_suspected else ""
        question = f"{prefix}approve {name} {approval_summary(self, name, args)}? [y/N/a=always/d=never] "
        answer = self._read_approval_answer(question)
        if answer in {"a", "always"}:
            # 只记在内存里：落盘的"上次批过"会变成永久后门。
            self._approval_memory[approval_class] = True
            return True
        if answer in {"d", "never"}:
            self._approval_memory[approval_class] = False
            return False
        return answer in {"y", "yes"}

    def reset(self):
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

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    def run_path(self, raw_path):
        """把路径锚定在**当前 run 目录**之下（read_artifact 专用）。

        为什么不复用 `path()`：run 目录在工作区里面，`path()` 会放行整个仓库，
        那样 read_artifact 就成了绕过 read_file 审计的另一条读文件通道。
        跨 run 也不允许——模型只该看见自己这次运行的证据。
        """
        run_dir = getattr(self, "current_run_dir", None)
        if run_dir is None:
            raise ValueError("no active run directory")
        run_root = Path(run_dir).resolve()
        path = Path(raw_path)
        path = path if path.is_absolute() else run_root / path
        resolved = path.resolve()
        if os.path.commonpath([str(run_root), str(resolved)]) != str(run_root):
            raise ValueError(f"path escapes run directory: {raw_path}")
        return resolved

    def compact_context(self, trigger="context_pressure"):
        """把较早的历史压成一份结构化交接，返回 artifact（没压则返回 None）。

        在链路里的位置：主循环发现上下文吃紧（占用率过高 / 历史连续被削 /
        软预算命中 / prompt 根本发不出去）时调用它，然后重新组一次 prompt。
        """
        if self.compaction_mode == "off":
            return None
        task_state = self.current_task_state
        if task_state is None:
            return None
        history = list(self.session.get("history", []))
        pending = [item for item in history if item.get("pending")]
        # 当前用户请求不参与压缩：它每轮都要作为 Current user request 出现。
        compactable = [item for item in history if not item.get("pending")]
        artifact, remaining = compactionlib.compact(
            compactable,
            self.run_store.read_trace(task_state.run_id),
            method=self.compaction_mode,
            budget=int(self.context_manager.section_budgets.get("history", 1200)),
            aux_client=self.aux_model_client(),
            run_id=task_state.run_id,
            measure=self.context_manager.measure,
        )
        if artifact is None:
            return None
        covered_count = artifact.covered_history_count
        covered = compactable[:covered_count]
        if all(item.get("compaction") for item in covered):
            # 压过的区间再压一次只会产出一份"摘要的摘要"，既没有新信息，
            # 又让 covered_range 变得没法追溯。
            return None
        compactions = self.session.setdefault("compactions", [])
        raw_path, _ = self.run_store.write_context_turns(
            task_state.run_id,
            len(compactions) + 1,
            [self.redact_artifact(dict(item)) for item in covered],
        )
        artifact = replace(artifact, raw_path=raw_path)
        summary = compactionlib.render_compaction(
            artifact, int(self.context_manager.section_budgets.get("history", 1200))
        )
        summary_entry = {
            "role": "system",
            "content": summary,
            "created_at": now(),
            "compaction": artifact.id,
        }
        # 顺序：摘要 -> 当前请求 -> 未压缩的最近步骤。这是时间顺序，
        # 当前请求确实发生在这些最近步骤之前。
        self.session["history"] = [summary_entry, *pending, *remaining]
        compactions.append(artifact.to_dict())
        self.session_path = self.session_store.save(self.session)
        self.history_reduction_streak = 0
        return artifact

    def aux_model_client(self):
        """给 compaction 模型模式用的辅助后端。默认就用主后端。"""
        return getattr(self, "_aux_model_client", None) or self.model_client

    def offload_request(self, user_message):
        """当前请求本身就装不下时，把它落盘并换成摘要 + 指针。

        为什么不裁剪它：`当前请求永不裁剪` 这条原则的意义在于，被砍掉的那半句
        很可能正是任务的关键约束。落盘之后模型可以自己分段读回来。
        """
        text = str(user_message)
        stored = self.store_tool_artifact("user_request", self.redact_text(text))
        if stored is None:
            return ""
        path, lines = stored
        head = clip(text, 1500, keep="head")
        return (
            f"{head}\n"
            f'... this request is {lines} line(s) long and was offloaded; '
            f'read the rest with read_artifact("{path}", start, end).'
        )

    def store_tool_artifact(self, tool_name, text):
        """把一份超阈值的工具输出卸载到 run 目录，返回 (相对路径, 行数)。

        没有活跃 run（比如直接调 run_tool 的测试和评测）时返回 None，
        调用方退回原来的硬截断——卸载是增强，不该成为新的失败点。
        """
        task_state = getattr(self, "current_task_state", None)
        if task_state is None or getattr(self, "current_run_dir", None) is None:
            return None
        with self._artifact_lock:
            self._artifact_seq += 1
            sequence = self._artifact_seq
        try:
            return self.run_store.write_artifact(task_state.run_id, sequence, tool_name, text)
        except OSError:
            return None
