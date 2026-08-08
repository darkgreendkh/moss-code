"""Tool execution, approval, planning and artifact operations."""

import json
import os
import sys
from pathlib import Path

from moss import atomic_io
from moss.agent import budget as budgetlib
from moss.agent import stall as stalllib
from moss.agent.verification import is_verification_command
from moss.context.token_budget import (
    clip,
)
from moss.execution import registry as toolkit
from moss.execution.executor import approval_summary
from moss.execution.protocol import ToolContext
from moss.execution.safety import shell as shell_policy
from moss.extensions import code_mode as code_modelib
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

# 跨会话持久化的审批记忆落在这里（.moss/ 本就 gitignored）。
APPROVALS_FILENAME = "approvals.json"
# 允许类决定只在"低风险读类"上跨会话记住：run_shell 的 read_only/test 档，
# 以及非 risky 工具（archival 意义不大，但无害）。写/网络/高危一律不持久——
# 把 "always allow git status" 记住是便利，把 "always allow rm -rf" 记住是灾难。
_PERSISTABLE_ALLOW_RISKS = frozenset({"read_only", "test", "low"})


def _approvals_path(root):
    return Path(root) / ".moss" / APPROVALS_FILENAME


def _approval_persistable(approval_class, allowed):
    """能不能把这条决定写进磁盘。拒绝(deny)一律可持久（保守，永远是收紧）；
    允许(allow)只有低风险读类才行。"""
    _name, risk, _bucket = approval_class
    if not allowed:
        return True
    return risk in _PERSISTABLE_ALLOW_RISKS


def load_persisted_approvals(root):
    """从磁盘读回跨会话的审批决定。文件缺失/损坏/被篡改都退回空表——
    读不出的持久许可绝不能变成"默默放行"。加载时**重新按风险校验**一遍，
    防止有人手改文件把一条高危 allow 塞进来。"""
    memory = {}
    try:
        data = json.loads(_approvals_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return memory
    for entry in (data or {}).get("decisions", []):
        try:
            key = (str(entry["name"]), str(entry["risk"]), str(entry.get("bucket", "")))
            allowed = bool(entry["allowed"])
        except (KeyError, TypeError):
            continue
        if _approval_persistable(key, allowed):
            memory[key] = allowed
    return memory


def save_persisted_approvals(root, memory):
    """把当前会话里够格持久化的决定原子写回磁盘。写失败静默——审批记忆
    是加速项，落不了盘顶多下次再问一遍，绝不能因此拦住工具执行。"""
    decisions = [
        {"name": name, "risk": risk, "bucket": bucket, "allowed": allowed}
        for (name, risk, bucket), allowed in memory.items()
        if _approval_persistable((name, risk, bucket), allowed)
    ]
    path = _approvals_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.write_atomic(
            str(path), json.dumps({"decisions": decisions}, ensure_ascii=False, indent=2)
        )
    except OSError:
        pass


def clear_persisted_approvals(root):
    """删掉持久化文件（/approvals clear 的一部分）。"""
    try:
        _approvals_path(root).unlink()
    except OSError:
        pass


class ExecutionService:
    def __init__(self, agent):
        self.agent = agent

    def build_tools(self):
        self = self.agent
        return toolkit.build_tool_registry(self.tool_context())

    def native_tool_definitions(self):
        self = self.agent
        native_tool_format = str(
            getattr(self.model_client, "native_tool_format", "") or ""
        )
        if not native_tool_format:
            return None
        return toolkit.native_tool_definitions(self.tools, native_tool_format)

    def resolved_tool_protocol(self):
        self = self.agent
        if self.tool_protocol != "auto":
            return self.tool_protocol
        capabilities = getattr(self.model_client, "capabilities", None)
        supports_native = bool(
            (capabilities.supports_native_tools if capabilities is not None else False)
            or getattr(self.model_client, "supports_native_tools", False)
        )
        return "native" if supports_native else "text"

    def ask(self, user_message):
        self = self.agent
        from moss.agent.loop import AgentLoop

        return AgentLoop(self).run(user_message)

    def execute_tool(self, name, args, defer_side_effects=False):
        self = self.agent
        result = self.tool_executor.execute(
            name, args, defer_side_effects=defer_side_effects
        )
        self._last_tool_result_metadata = dict(result.metadata)
        return result

    def execute(self, request):
        """唯一的结构化执行入口，供 MCP server / hooks / 评测使用。

        为什么需要它：把 `tool_*` 收成私有之后，外部集成必须有一个受护栏的入口，
        否则大家会退回去直接调 toolkit——那正是这次要堵的口子。
        request 可以是 ActionRequest，也可以是 {"name":..., "args":...} 字典。
        """
        self = self.agent
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
        self = self.agent
        return self.execute_tool(name, args).content

    def repeated_tool_call(self, name, args):
        self = self.agent
        tool_events = [
            item for item in self.session["history"] if item["role"] == "tool"
        ]
        return stalllib.is_repeated_call(tool_events, name, args)

    def set_plan(self, plan):
        """把模型给的计划写进 task_state，并落 trace。

        计划是"显式的意图声明"：跑偏时能对照它看出偏在哪一步，
        而不是只能靠步数上限兜底。plan_drift 的判定在 spec-08 离线做，
        主循环只负责把它记下来。
        """
        self = self.agent
        self.current_plan = list(plan or [])
        task_state = self.current_task_state
        if task_state is not None:
            task_state.plan = list(self.current_plan)
            self.emit_trace(
                task_state,
                trace_events.PLAN_UPDATED,
                {"steps": list(self.current_plan)},
            )
        self._plan_step_started_at = (
            self.current_task_state.tool_steps if task_state else 0
        )
        self._plan_pressure_reported = set()
        return self.current_plan

    def render_plan_text(self):
        self = self.agent
        return toolkit.render_plan(self.current_plan)

    def check_plan_pressure(self):
        """某个 in_progress 步骤吃掉的步数远超均摊预算时，建议重规划。

        软预算而不是硬拦截：计划本来就会变，真正的问题是"卡在同一步却不承认"。
        """
        self = self.agent
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
        return {
            "step_id": current["id"],
            "title": current["title"],
            "spent_steps": spent,
        }

    def new_run_budget(self):
        """给一次运行开一份预算账本。"""
        self = self.agent
        limits = dict(self.run_budget_limits)
        return budgetlib.RunBudget(max_steps=0, **limits)

    def price_for_usage(self, input_tokens, output_tokens):
        """把 token 数换算成金额；查不到价格返回 None。

        返回 None 而不是 0：把未知当成 0 会让金额上限永远不触发，
        正好在最该拦的时候不拦。价目表在 spec-08 的 evaluation/pricing.py 落地，
        这里先留出接入点。
        """
        self = self.agent
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
        self = self.agent
        return list(self._tool_outcomes)

    def record_tool_outcome(self, name, args, metadata):
        self = self.agent
        verified = is_verification_command(name, args, metadata)
        self._tool_outcomes.append(
            {
                "name": name,
                "args": args,
                "workspace_changed": bool((metadata or {}).get("workspace_changed")),
                "tool_error_code": str(
                    (metadata or {}).get("tool_error_code", "") or ""
                ),
                "verification": verified,
            }
        )
        del self._tool_outcomes[:-STALL_EVENT_HISTORY]
        # 收尾摘要用：affected_paths 会随窗口滚动被 _tool_outcomes 丢掉，所以
        # 单独按 run 累加一份改动文件集合。验证只要成功跑过一次就置位——
        # verify 失败（error）不算"验证过"，否则收尾会谎报已验证。
        for rel_path in (metadata or {}).get("affected_paths", ()) or ():
            self.run_changed_paths.add(str(rel_path))
        if verified and str((metadata or {}).get("tool_error_code", "") or "") == "":
            self.run_verified = True

    def detect_stall(self):
        self = self.agent
        return stalllib.detect_stall(self.stall_events())

    def tool_example(self, name):
        self = self.agent
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        self = self.agent
        toolkit.validate_tool(self.tool_context(), name, args)

    def tool_context(self):
        self = self.agent
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_delegate=self.spawn_delegate,
            skills_provider=lambda: self.skills,
            skill_activator=self.activate_skill,
            mcp_tools_provider=lambda: self.mcp_tools,
            code_mode_enabled=self.code_mode_enabled(),
            guarded_tool_runner=self.run_tool,
            catalog_threshold=self.tool_catalog_threshold,
            tool_registry_provider=lambda: self.tools,
            cancel_token=self.cancel_token,
            plan_writer=self.set_plan,
            memory_writer=self.memory_write_action,
            memory_updater=self.memory_update_action,
            memory_deleter=self.memory_delete_action,
            memory_searcher=self.memory_search_action,
            sandbox_plan=self.sandbox_plan,
            run_path_resolver=self.run_path,
        )

    def code_mode_enabled(self):
        self = self.agent
        return bool(getattr(self, "_code_mode_enabled", False))

    def _resolve_code_mode(self):
        """code mode 到底能不能用：显式开关 **且** 沙箱可用。

        开了但沙箱不可用时打一次 stderr——静默不给工具的话，用户会以为
        自己开了却一直没见模型用过，然后去查 prompt。
        """
        self = self.agent
        if not getattr(self, "code_mode", False):
            return False
        if code_modelib.sandbox_ready(self.sandbox_plan):
            return True
        print(
            "warning: --enable-code-mode was requested but no sandbox is available; run_orchestration stays disabled",
            file=sys.stderr,
        )
        return False

    def capability_set(self):
        """本 agent 当前真正握有的能力集合（工具声明的并集，按策略过滤）。

        子 agent 的能力必须是它的子集——判断"有没有越权"要有一个明确的被比较对象，
        不能靠"看起来是只读的"。
        """
        self = self.agent
        capabilities = set()
        for tool in self.tools.values():
            capabilities |= set(tool.get("capabilities") or frozenset())
        if self.policy is not None and self.policy.read_only:
            capabilities &= {"fs_read"}
        return frozenset(capabilities)

    def _tool_list_files(self, args):
        self = self.agent
        return toolkit.tool_list_files(self.tool_context(), args)

    def _tool_read_file(self, args):
        self = self.agent
        return toolkit.tool_read_file(self.tool_context(), args)

    def _tool_search_text(self, args):
        self = self.agent
        return toolkit.tool_search_text(self.tool_context(), args)

    def _tool_run_shell(self, args):
        self = self.agent
        result = toolkit.tool_run_shell(self.tool_context(), args)
        return result.content if hasattr(result, "content") else result

    def _tool_write_file(self, args):
        self = self.agent
        return toolkit.tool_write_file(self.tool_context(), args)

    def _tool_edit_file(self, args):
        self = self.agent
        return toolkit.tool_edit_file(self.tool_context(), args)

    def _tool_delegate(self, args):
        self = self.agent
        return toolkit.tool_delegate(self.tool_context(), args)

    def _is_network_command(self, name, args):
        self = self.agent
        if name != "run_shell":
            return False
        return (
            toolkit.classify_shell_command((args or {}).get("command", "")).level
            == "network"
        )

    def flag_injection_suspected(self, finding):
        """记下一次注入嫌疑，并让本 run 剩余的 risky 工具强制走审批。

        为什么是"强制审批"而不是"拒绝"：检测必然有误报，拒绝会把正常任务
        直接打断；而强制审批只是把决定权交回给人，代价是一次确认。
        """
        self = self.agent
        self.injection_findings.append(finding)
        return finding

    @property
    def injection_suspected(self):
        self = self.agent
        return bool(self.injection_findings)

    def network_hosts_refused(self, name, args):
        """给了网络白名单时，命令里出现的域名必须在名单内。"""
        self = self.agent
        if name != "run_shell" or not self.allowed_network_hosts:
            return ()
        hosts = shell_policy.extract_hosts((args or {}).get("command", ""))
        return tuple(
            (
                host
                for host in hosts
                if not shell_policy.host_allowed(host, self.allowed_network_hosts)
            )
        )

    def approve(self, name, args):
        self = self.agent
        if self.read_only:
            return False
        refused = self.network_hosts_refused(name, args)
        if refused:
            return False
        if self.approval_policy == "auto" and self._is_network_command(name, args):
            return self._ask_for_approval(name, args)
        if self.approval_policy == "auto" and self.injection_suspected:
            return self._ask_for_approval(name, args)
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        return self._ask_for_approval(name, args)

    def approval_class(self, name, args):
        """ "本类"的定义：(工具, 风险等级, 路径桶)；shell 额外按 argv[0] 归类。

        粒度太粗（只按工具名）会让"允许一次 git status"变成"允许所有 shell"；
        太细（按完整参数）则等于没有记忆，用户还是每次都要按 y。
        """
        self = self.agent
        args = args or {}
        risk = "high"
        bucket = ""
        if name == "run_shell":
            classified = toolkit.classify_shell_command(args.get("command", ""))
            risk = classified.level
            bucket = (
                classified.argvs[0][0]
                if classified.argvs and classified.argvs[0]
                else ""
            )
        else:
            tool = self.tools.get(name) or {}
            risk = "high" if tool.get("risky") else "low"
            raw_path = str(args.get("path", "")).strip()
            if raw_path:
                bucket = raw_path.replace("\\", "/").lstrip("./").split("/", 1)[0]
        return (name, risk, bucket)

    def _read_approval_answer(self, question):
        """从 tty 读审批回答。

        为什么不用 input()：一旦有人 `echo task | moss`，stdin 就是管道，
        input() 会把**任务文本**当成审批回答读走——用户的第一行输入变成了 "y"。
        所以优先打开 /dev/tty；打不开就降级为拒绝，保持"读不清 = 不批准"。

        为什么把图例(`question`)当正文整块打完、只留一个极短的 `> ` 作真正的输入
        提示：`question` 是"原因/摘要/[y=once·a=always·d=never·N=no]"多行块，末行还带
        宽字符 `·`。直接把它整块喂给 input()，GNU readline 会按**单行**算 prompt 宽度、
        再横向滚动保证光标可见，把左边一截藏掉——用户只看到 `d = never · N = no`，
        以为只有这两个选项。/dev/tty 那条路虽不走 readline，但 raw 模式下 `\n` 只下移
        不回行首会让提示阶梯错位。两条都靠"图例进正文、输入提示只剩两字符"根治：
        readline 只需管 `> `，raw 模式靠 `\r\n` 强制每行回行首，开头 `\r`+空格清掉进度
        spinner 用 `\r` 留下的残迹。
        """
        self = self.agent
        banner = "\r" + " " * 72 + "\r" + question.rstrip("\n ").replace("\n", "\r\n") + "\r\n"
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write(banner + "> ")
                tty.flush()
                return (tty.readline() or "").strip().lower()
        except (OSError, UnicodeDecodeError):
            pass
        if not sys.stdin or not sys.stdin.isatty():
            return ""
        try:
            print(question.rstrip("\n "), file=sys.stderr, flush=True)
            return input("> ").strip().lower()
        except (EOFError, UnicodeDecodeError):
            return ""

    def _approval_prompt(self, name, args):
        """把审批提示排成"原因 / 摘要块 / 回答行"三段，别糊成一行。

        为什么改：写文件类摘要是最长 800 字符的多行 diff，过去和 `? [y/N...]`
        挤在同一行，用户要在一坨 diff 末尾去找那个问号。分行之后 diff 独占
        一块、回答提示单独落在最后一行，一眼就知道在问什么、按什么。
        """
        self = self.agent
        lines = []
        injection = self.injection_suspected and self.injection_findings
        if injection:
            # 说清"为什么可疑"：命中的模式名 + 原文 + 来源。只给模式名的话，用户没法
            # 判断这是"读到了自己项目文档里的示例字符串"这种误报，还是真有一段外部
            # 文本在指挥 agent——两者的正确处置完全相反。
            finding = self.injection_findings[-1]
            pattern = str(getattr(finding, "pattern", "") or "").replace("_", " ")
            reason = f" (matched: {pattern})" if pattern else ""
            lines.append(f"! prompt-injection suspected in earlier tool output{reason} — review before approving")
            source = str(getattr(finding, "source", "") or "").strip()
            if source:
                lines.append(f"    source: {source}")
            excerpt = str(getattr(finding, "excerpt", "") or "").strip()
            if excerpt:
                # excerpt 是不可信原文，打到终端前必须脱敏。
                lines.append(f"    matched text: {self.redact_text(excerpt)}")
        lines.append(f"approve {name}?")
        detail = approval_summary(self, name, args)
        for detail_line in detail.split("\n"):
            lines.append(f"    {detail_line}" if detail_line else "")
        if injection:
            # 注入嫌疑下的审批是针对"这次可疑输出"的一次性判断，不提供 always/never：
            # 把一个临时安全信号变成对整个工具类的持久决定，只会误伤（用户看着"疑似
            # 注入"按下的 never 会把这类命令在本会话里永久禁掉）。
            lines.append("[y = once · N = no] ")
        else:
            lines.append("[y = once · a = always · d = never · N = no] ")
        return "\n".join(lines)

    def remembered_approvals(self):
        """本会话里"总是允许/总是拒绝"过的审批类。供 /approvals 查看。"""
        self = self.agent
        return dict(self._approval_memory)

    def clear_approval_memory(self):
        """清空记住的审批决定，返回清掉的条数。误按了 always 时的后悔药。

        持久化文件也一并删掉——否则清完内存，下次启动又从磁盘把它读回来，
        "后悔药"就失效了。
        """
        self = self.agent
        count = len(self._approval_memory)
        self._approval_memory.clear()
        clear_persisted_approvals(self.root)
        return count

    def _ask_for_approval(self, name, args):
        self = self.agent
        # 注入嫌疑期间：既不读也不写审批记忆，每次都重新问。理由是这时的批准/拒绝是
        # 针对"这次可疑输出"的一次性判断，不是对整个工具类的持久决定——若读记忆，之前
        # 记过的 always 会让可疑动作直接跳过审批（等于注入警戒形同虚设）；若写记忆，
        # 用户被"疑似注入"吓到按下的 never 会把这类命令在本会话里永久禁掉。两个方向都错。
        injection = self.injection_suspected
        approval_class = self.approval_class(name, args)
        if not injection:
            remembered = self._approval_memory.get(approval_class)
            if remembered is not None:
                return remembered
        question = self._approval_prompt(name, args)
        answer = self._read_approval_answer(question)
        if answer in {"a", "always"}:
            if not injection:
                self._approval_memory[approval_class] = True
                save_persisted_approvals(self.root, self._approval_memory)
            return True
        if answer in {"d", "never"}:
            if not injection:
                self._approval_memory[approval_class] = False
                save_persisted_approvals(self.root, self._approval_memory)
            return False
        return answer in {"y", "yes"}

    def path(self, raw_path):
        self = self.agent
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    def run_path(self, raw_path):
        """把路径锚定在**当前 run 目录**之下（read_artifact 专用）。

        为什么不复用 `path()`：run 目录在工作区里面，`path()` 会放行整个仓库，
        那样 read_artifact 就成了绕过 read_file 审计的另一条读文件通道。
        跨 run 也不允许——模型只该看见自己这次运行的证据。
        """
        self = self.agent
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

    def offload_request(self, user_message):
        """当前请求本身就装不下时，把它落盘并换成摘要 + 指针。

        为什么不裁剪它：`当前请求永不裁剪` 这条原则的意义在于，被砍掉的那半句
        很可能正是任务的关键约束。落盘之后模型可以自己分段读回来。
        """
        self = self.agent
        text = str(user_message)
        stored = self.store_tool_artifact("user_request", self.redact_text(text))
        if stored is None:
            return ""
        (path, lines) = stored
        head = clip(text, 1500, keep="head")
        return f'{head}\n... this request is {lines} line(s) long and was offloaded; read the rest with read_artifact("{path}", start, end).'

    def store_tool_artifact(self, tool_name, text):
        """把一份超阈值的工具输出卸载到 run 目录，返回 (相对路径, 行数)。

        没有活跃 run（比如直接调 run_tool 的测试和评测）时返回 None，
        调用方退回原来的硬截断——卸载是增强，不该成为新的失败点。
        """
        self = self.agent
        task_state = getattr(self, "current_task_state", None)
        if task_state is None or getattr(self, "current_run_dir", None) is None:
            return None
        with self._artifact_lock:
            self._artifact_seq += 1
            sequence = self._artifact_seq
        try:
            return self.run_store.write_artifact(
                task_state.run_id, sequence, tool_name, text
            )
        except OSError:
            return None
