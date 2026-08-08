"""Agent 运行时核心逻辑。

Moss 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import sys
import threading
import warnings
import uuid
from datetime import datetime
from pathlib import Path

from .extensions import router as model_routerlib
from .extensions.mcp import registry as mcplib
from .memory import service as memorylib
from .execution.safety import secrets as securitylib
from .context.manager import ContextManager
from .runs.store import RunStore
from .runs.session import SessionStore
from .execution.executor import ToolExecutor
from .execution import registry as toolkit
from .clock import now
from .context.token_budget import (
    TokenCalibrationStore,
)
from .execution.safety import policy as policylib
from .execution.safety import sandbox as sandboxlib
from .runs.observability import events as trace_events
from .context.service import ContextService
from .execution.service import ExecutionService, load_persisted_approvals
from .extensions.manager import ExtensionManager
from .runs.coordinator import RunCoordinator

from .context.repository.workspace import (
    SnapshotResult,
)

DEFAULT_SHELL_ENV_ALLOWLIST = (
    # POSIX-ish
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
    # Windows: cmd.exe (通过 shell=True 启动) 没有这些变量就直接崩，
    # `run_shell` 在 Windows 上原本几乎不可用。
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
    DURABLE_TRACE_EVENTS = frozenset(
        {
            trace_events.RUN_FINISHED,
            trace_events.CHECKPOINT_CREATED,
            trace_events.RUN_INTERRUPTED,
        }
    )

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
        aux_model_client=None,
        code_mode=False,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.context_service = ContextService(self)
        self.execution_service = ExecutionService(self)
        self.run_coordinator = RunCoordinator(self)
        self.extension_manager = ExtensionManager(self)
        self.invocation_cwd = Path(
            getattr(workspace, "invocation_cwd", None) or workspace.cwd
        )
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
        # 多模型路由（spec-09 §9.7）：脏活走 aux，主线走主模型。
        # aux_model_client=None 时全部回落主模型，行为与加路由前逐字节一致。
        self._aux_model_client = aux_model_client
        self.model_router = model_routerlib.ModelRouter(
            model_client, aux_model_client, observer=self._note_model_route
        )
        # 历史段连续几轮触发了收缩。连续两轮说明不是偶发的大输出，
        # 而是历史本身已经装不下了 —— 那是该压缩而不是继续削的信号。
        self.history_reduction_streak = 0
        self.sandbox_plan = sandboxlib.announce(sandboxlib.detect(sandbox))
        # 审批决定的记忆：{(工具, 风险, 路径桶): 是否允许}。启动时从磁盘读回上次
        # "总是允许/总是拒绝"过的决定——但只有低风险读类的 allow 才会被持久化
        # (写/网络/高危永不落盘，见 execution/service.py)，所以这里读回来的不会是
        # 后门；拒绝(deny)一律持久，因为它永远是收紧。加载时再按风险校验一遍，
        # 防篡改。/approvals clear 会同时清掉内存与磁盘。
        self._approval_memory = load_persisted_approvals(self.root)
        # 能力/路径策略。read_only 也归它管，这样"只读"不再是散落在多处的特判。
        self.policy = (
            policy
            if policy is not None
            else policylib.Policy.build(read_only=read_only)
        )
        # 多维预算的上限（步数之外还有 token / 时间 / 金额）。默认全 None，
        # 行为与加预算前完全一致。
        self.run_budget_limits = dict(run_budget_limits or {})
        self.last_run_budget = None
        # 一次 ask() 里被改动过的文件集合 + 是否跑过验证。收尾摘要要回答
        # "这一轮到底动了哪些文件、验证了没有"——数据本来散在每次工具的
        # metadata 里，这里按 run 攒一份，begin_run() 清空。
        self.run_changed_paths = set()
        self.run_verified = False
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
        self.shell_env_allowlist = tuple(
            shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST
        )
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update(
                {str(key): bool(value) for key, value in feature_flags.items()}
            )
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        self.run_store = run_store or RunStore(
            Path(workspace.repo_root) / ".moss" / "runs",
            # 崩溃后对账要看当前磁盘上的文件，所以把路径锚定函数交给它。
            workspace_path=self.path,
        )
        # 上一条动作回执的 id。intent 的 idempotency_key 由它和参数摘要算出，
        # 同一个动作重放时算出同一把钥匙，于是不会产生两条 receipt。
        self.last_action_receipt_id = ""
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
        # 外部 MCP server 在**启动期**连一次，工具落进白名单（spec-09 §9.2）。
        # 连不上不让 agent 起不来，但失败要打到 stderr——静默少几个工具，
        # 表现是模型莫名其妙做不成事。
        self.mcp_tools, self.mcp_clients, self.tool_catalog_threshold = (
            mcplib.build_mcp_tools(
                self.root,
                on_error=lambda message: print(f"warning: {message}", file=sys.stderr),
            )
        )
        # code mode（spec-09 §9.3）。默认关闭，且沙箱不可用时即使开了也不给——
        # AST 白名单是第一道，OS 隔离是第二道，只有第一道的话一个没想到的
        # 逃逸路径就是完整的任意代码执行。
        self.code_mode = bool(code_mode)
        # 只判一次并缓存：这个判定会打 stderr 警告，而 tool_context() 是
        # 每次工具调用都要建的——每调一次工具喊一遍等于把警告变成噪声。
        self._code_mode_enabled = self._resolve_code_mode()
        # 本 run 跑过的钩子。进 report，因为 pre_tool 的拒绝会改变控制流。
        self.hook_outcomes = []
        # 当前点亮的 skill（use_skill 写入）。它的 allowed-tools 是**临时**覆盖：
        # 换 skill 或 run 结束即失效。落盘的"永久放开"会变成一个没人记得的后门。
        self.active_skill = None
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
        # 恢复出来的会话要把计划一起带回来，否则 agent 会忘掉自己刚才打算怎么做。
        self.current_plan = list(
            (self.current_checkpoint() or {}).get("plan", []) or []
        )
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
        # 回放客户端把未命中回调给 runtime，这样 miss 会进 trace 而不只是 stderr
        # 一行——评测要按 run 统计"这次回放到底有多少步偏离了磁带"。
        if getattr(model_client, "miss_observer", "unset") is None:
            model_client.miss_observer = self.note_replay_miss

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
        return self.run_coordinator.current_runtime_identity()

    def checkpoint_state(self):
        return self.run_coordinator.checkpoint_state()

    def current_checkpoint(self):
        return self.run_coordinator.current_checkpoint()

    def invalidate_stale_memory(self):
        return self.run_coordinator.invalidate_stale_memory()

    def evaluate_resume_state(self):
        return self.run_coordinator.evaluate_resume_state()

    def render_checkpoint_text(self):
        return self.run_coordinator.render_checkpoint_text()

    def rewind(self, steps=1, force=False):
        return self.run_coordinator.rewind(steps, force)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return self.execution_service.build_tools()

    def native_tool_definitions(self):
        return self.execution_service.native_tool_definitions()

    def resolved_tool_protocol(self):
        return self.execution_service.resolved_tool_protocol()

    def recover_interrupted_runs(self):
        return self.run_coordinator.recover_interrupted_runs()

    def build_skills(self):
        return self.extension_manager.build_skills()

    def skill_trust_store(self):
        return self.extension_manager.skill_trust_store()

    def effective_allowed_tools(self):
        return self.extension_manager.effective_allowed_tools()

    def activate_skill(self, name):
        return self.extension_manager.activate_skill(name)

    def _confirm_skill(self, question):
        return self.extension_manager._confirm_skill(question)

    def skill_scope_hint(self):
        return self.extension_manager.skill_scope_hint()

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        return self.extension_manager._apply_tool_allowlist(tools)

    def tool_signature(self):
        return self.extension_manager.tool_signature()

    def build_prefix(self):
        return self.context_service.build_prefix()

    def _apply_prefix_state(self, prefix_state):
        return self.context_service._apply_prefix_state(prefix_state)

    def reload_registry(self):
        return self.extension_manager.reload_registry()

    def begin_run(self):
        return self.run_coordinator.begin_run()

    def end_run(self):
        return self.run_coordinator.end_run()

    def _detect_registry_drift(self):
        return self.extension_manager._detect_registry_drift()

    def refresh_prefix(self, force=False):
        return self.context_service.refresh_prefix(force)

    def memory_text(self):
        return self.context_service.memory_text()

    def history_text(self):
        return self.context_service.history_text()

    def feature_enabled(self, name):
        return self.context_service.feature_enabled(name)

    def token_measure(self):
        return self.context_service.token_measure()

    def record_token_usage_sample(self, estimated_tokens, actual_tokens):
        return self.context_service.record_token_usage_sample(
            estimated_tokens, actual_tokens
        )

    def prompt(self, user_message):
        return self.context_service.prompt(user_message)

    def clear_pending_history(self):
        return self.context_service.clear_pending_history()

    def record(self, item):
        return self.context_service.record(item)

    def start_run(self, task_state):
        return self.run_coordinator.start_run(task_state)

    def heartbeat_run(self, task_state):
        return self.run_coordinator.heartbeat_run(task_state)

    def release_run(self, task_state):
        return self.run_coordinator.release_run(task_state)

    def write_task_state(self, task_state):
        return self.run_coordinator.write_task_state(task_state)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return self.context_service.is_secret_env_name(name)

    def configured_secret_env_items(self):
        return self.context_service.configured_secret_env_items()

    def detected_secret_env_items(self):
        return self.context_service.detected_secret_env_items()

    def secret_env_summary(self):
        return self.context_service.secret_env_summary()

    def detected_secret_env_summary(self):
        return self.context_service.detected_secret_env_summary()

    def redact_text(self, text):
        return self.context_service.redact_text(text)

    def redact_artifact(self, value, key=None):
        return self.context_service.redact_artifact(value, key)

    def shell_env(self):
        return self.context_service.shell_env()

    def prompt_metadata(self, user_message, prompt):
        return self.context_service.prompt_metadata(user_message, prompt)

    def _build_prompt_and_metadata(self, user_message):
        return self.context_service._build_prompt_and_metadata(user_message)

    def _build_prompt_bundle_and_metadata(self, user_message):
        return self.context_service._build_prompt_bundle_and_metadata(user_message)

    def build_context_result(self, user_message):
        return self.context_service.build_context_result(user_message)

    def emit_progress(self, event, payload=None):
        return self.run_coordinator.emit_progress(event, payload)

    def emit_trace(self, task_state, event, payload=None):
        return self.run_coordinator.emit_trace(task_state, event, payload)

    def fire_hook(self, point, payload):
        return self.extension_manager.fire_hook(point, payload)

    def note_replay_miss(self, record):
        return self.run_coordinator.note_replay_miss(record)

    def record_memory_event(self, event, payload=None):
        return self.context_service.record_memory_event(event, payload)

    def memory_source_refs(self):
        return self.context_service.memory_source_refs()

    @staticmethod
    def _memory_rejection(reason):
        return {"status": "rejected", "reason": str(reason)}

    def memory_write_action(self, args):
        return self.context_service.memory_write_action(args)

    def memory_update_action(self, args):
        return self.context_service.memory_update_action(args)

    def memory_delete_action(self, args):
        return self.context_service.memory_delete_action(args)

    def memory_search_action(self, args):
        return self.context_service.memory_search_action(args)

    def _save_memory_state(self):
        return self.context_service._save_memory_state()

    def attach_repo_map(self, workspace):
        return self.context_service.attach_repo_map(workspace)

    def relevant_file_anchors(self, user_message, limit=5):
        return self.context_service.relevant_file_anchors(user_message, limit)

    def note_anchor_outcome(self, path):
        return self.context_service.note_anchor_outcome(path)

    def note_nearby_instructions(self, path):
        return self.context_service.note_nearby_instructions(path)

    def drain_instruction_notices(self):
        return self.context_service.drain_instruction_notices()

    def workspace_ignore_rules(self):
        return self.context_service.workspace_ignore_rules()

    def capture_workspace_snapshot(self):
        return self.context_service.capture_workspace_snapshot()

    def diff_workspace_snapshots(self, before, after):
        return self.context_service.diff_workspace_snapshots(before, after)

    def snapshot_strategy(self):
        return self.context_service.snapshot_strategy()

    def create_checkpoint(self, task_state, user_message, trigger):
        return self.run_coordinator.create_checkpoint(task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return self.context_service.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        return self.context_service.update_memory_after_tool(name, args, result)

    def record_process_note_for_tool(self, name, metadata):
        return self.context_service.record_process_note_for_tool(name, metadata)

    def promote_durable_memory(self, user_message, final_answer):
        return self.context_service.promote_durable_memory(user_message, final_answer)

    def _reflection_summarizer(self):
        return self.context_service._reflection_summarizer()

    def distill_current_run(self):
        return self.context_service.distill_current_run()

    def ask(self, user_message):
        return self.execution_service.ask(user_message)

    def execute_tool(self, name, args, defer_side_effects=False):
        return self.execution_service.execute_tool(name, args, defer_side_effects)

    def execute(self, request):
        return self.execution_service.execute(request)

    def run_tool(self, name, args):
        return self.execution_service.run_tool(name, args)

    def repeated_tool_call(self, name, args):
        return self.execution_service.repeated_tool_call(name, args)

    def set_plan(self, plan):
        return self.execution_service.set_plan(plan)

    def render_plan_text(self):
        return self.execution_service.render_plan_text()

    def check_plan_pressure(self):
        return self.execution_service.check_plan_pressure()

    def new_run_budget(self):
        return self.execution_service.new_run_budget()

    def price_for_usage(self, input_tokens, output_tokens):
        return self.execution_service.price_for_usage(input_tokens, output_tokens)

    def stall_events(self):
        return self.execution_service.stall_events()

    def record_tool_outcome(self, name, args, metadata):
        return self.execution_service.record_tool_outcome(name, args, metadata)

    def detect_stall(self):
        return self.execution_service.detect_stall()

    @staticmethod
    def new_task_id():
        return (
            "task_"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

    @staticmethod
    def new_run_id():
        return (
            "run_"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

    def build_report(self, task_state):
        return self.run_coordinator.build_report(task_state)

    def summarize_run(self, task_state):
        return self.run_coordinator.summarize_run(task_state)

    def replay_summary(self):
        return self.run_coordinator.replay_summary()

    def tool_example(self, name):
        return self.execution_service.tool_example(name)

    def validate_tool(self, name, args):
        return self.execution_service.validate_tool(name, args)

    def tool_context(self):
        return self.execution_service.tool_context()

    def delegate_session_store(self):
        return self.extension_manager.delegate_session_store()

    def code_mode_enabled(self):
        return self.execution_service.code_mode_enabled()

    def _resolve_code_mode(self):
        return self.execution_service._resolve_code_mode()

    def capability_set(self):
        return self.execution_service.capability_set()

    def delegate_contract(self, goal, args=None):
        return self.extension_manager.delegate_contract(goal, args)

    def spawn_delegate(self, args):
        return self.extension_manager.spawn_delegate(args)

    def run_delegate(self, contract):
        return self.extension_manager.run_delegate(contract)

    def _delegate_anchor_exists(self, raw_path):
        return self.extension_manager._delegate_anchor_exists(raw_path)

    def _tool_list_files(self, args):
        return self.execution_service._tool_list_files(args)

    def _tool_read_file(self, args):
        return self.execution_service._tool_read_file(args)

    def _tool_search_text(self, args):
        return self.execution_service._tool_search_text(args)

    def _tool_run_shell(self, args):
        return self.execution_service._tool_run_shell(args)

    def _tool_write_file(self, args):
        return self.execution_service._tool_write_file(args)

    def _tool_edit_file(self, args):
        return self.execution_service._tool_edit_file(args)

    def _tool_delegate(self, args):
        return self.execution_service._tool_delegate(args)

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
        return self.execution_service._is_network_command(name, args)

    def flag_injection_suspected(self, finding):
        return self.execution_service.flag_injection_suspected(finding)

    @property
    def injection_suspected(self):
        return self.execution_service.injection_suspected

    def network_hosts_refused(self, name, args):
        return self.execution_service.network_hosts_refused(name, args)

    def approve(self, name, args):
        return self.execution_service.approve(name, args)

    def approval_class(self, name, args):
        return self.execution_service.approval_class(name, args)

    def remembered_approvals(self):
        return self.execution_service.remembered_approvals()

    def clear_approval_memory(self):
        return self.execution_service.clear_approval_memory()

    def _approval_prompt(self, name, args):
        return self.execution_service._approval_prompt(name, args)

    def _read_approval_answer(self, question):
        return self.execution_service._read_approval_answer(question)

    def _ask_for_approval(self, name, args):
        return self.execution_service._ask_for_approval(name, args)

    def reset(self):
        return self.run_coordinator.reset()

    def resume(self, session_id):
        return self.run_coordinator.resume(session_id)

    def path(self, raw_path):
        return self.execution_service.path(raw_path)

    def run_path(self, raw_path):
        return self.execution_service.run_path(raw_path)

    def compact_context(self, trigger="context_pressure"):
        return self.context_service.compact_context(trigger)

    def aux_model_client(self, task_kind="compaction"):
        return self.extension_manager.aux_model_client(task_kind)

    def _note_model_route(self, record):
        return self.extension_manager._note_model_route(record)

    def offload_request(self, user_message):
        return self.execution_service.offload_request(user_message)

    def store_tool_artifact(self, tool_name, text):
        return self.execution_service.store_tool_artifact(tool_name, text)
