"""Skill, hook, delegation and auxiliary-model extensions."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from moss.agent.state import STOP_REASON_FINAL_ANSWER_RETURNED
from moss.context.prefix import skill_signature, tool_signature
from moss.execution import registry as toolkit
from moss.extensions import delegation as delegationlib
from moss.extensions import hooks as hookslib
from moss.extensions import skills as skilllib
from moss.runs.observability import events as trace_events
from moss.runs.session import SessionStore

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


class ExtensionManager:
    def __init__(self, agent):
        self.agent = agent

    def build_skills(self):
        self = self.agent
        return skilllib.build_skill_registry(self.root)

    def skill_trust_store(self):
        self = self.agent
        return skilllib.SkillTrustStore(self.root / skilllib.TRUST_FILE)

    def effective_allowed_tools(self):
        """本次调用真正生效的工具白名单。

        run 级 allowlist 与 skill 的 `allowed-tools` 叠加。skill 只能在
        run 级名单之内收紧——这层判定在 `activate_skill` 就 fail-closed 做过了，
        这里只是把结果应用上。
        """
        self = self.agent
        override = (self.active_skill or {}).get("allowed_tools")
        if override is None:
            return self.allowed_tools
        return tuple(sorted(override))

    def activate_skill(self, name):
        """点亮一个 skill：校验供应链与能力声明，返回要注入的正文。

        能力覆盖在这个 skill 被换掉或本次 run 结束时失效——
        落盘的"永久放开"会变成一个没人记得的后门。
        """
        self = self.agent
        skill = (self.skills or {}).get(name)
        if not skill:
            raise ValueError(f"unknown skill: {name}")
        trust = self.skill_trust_store()
        if trust.needs_confirmation(skill):
            question = f"skill '{name}' from {skill['source']} changed (sha256 {skill['sha256'][:12]}); allowed-tools: {', '.join(skill['allowed_tools']) or '(none)'}"
            if not self._confirm_skill(question):
                raise ValueError(
                    f"skill {name} was not confirmed after its contents changed"
                )
            trust.trust(skill)
        override = skilllib.validate_allowed_tools(
            skill, self.allowed_tools, toolkit.legal_tool_names()
        )
        self.active_skill = {
            "name": name,
            "allowed_tools": override,
            "sha256": skill["sha256"],
        }
        body = skill.get("body", "") or "(skill has no instructions)"
        if skill.get("resources"):
            body += (
                "\n\nResources (read them with read_file if you need them): "
                + ", ".join(skill["resources"])
            )
        return body

    def _confirm_skill(self, question):
        self = self.agent
        if self.approval_policy == "never":
            return False
        if self.approval_policy == "auto":
            return True
        return self._read_approval_answer(f"Trust changed skill? {question} [y/N] ")

    def skill_scope_hint(self):
        """当前在碰的路径下有哪些 skill 可用。只给一行提示，不注入全文。"""
        self = self.agent
        if not self.skills:
            return ""
        paths = list(
            self.memory.to_dict().get("working", {}).get("recent_files", []) or []
        )
        paths.extend(self.last_relevant_anchors or [])
        return skilllib.scope_hints(self.skills, paths)

    def _apply_tool_allowlist(self, tools):
        self = self.agent
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names() | set(tools)
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(self.allowed_tools)
        return {name: tool for (name, tool) in tools.items() if name in allowed}

    def tool_signature(self):
        self = self.agent
        return tool_signature(self.tools)

    def reload_registry(self):
        """立即重读 skill/tool 注册表；只允许在模型 run 之外调用。"""
        self = self.agent
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

    def _detect_registry_drift(self):
        self = self.agent
        if not self._run_active or not self._frozen_registry:
            return None
        live_skills = self.build_skills()
        live_skill_signature = skill_signature(live_skills)
        if live_skill_signature == self._frozen_registry["skill_signature"]:
            return None
        frozen_names = set(self._frozen_registry["skills"])
        live_names = set(live_skills)
        changed = sorted(
            (
                name
                for name in frozen_names & live_names
                if self._frozen_registry["skills"][name] != live_skills[name]
            )
        )
        return {
            "added": sorted(live_names - frozen_names),
            "removed": sorted(frozen_names - live_names),
            "changed": changed,
        }

    def fire_hook(self, point, payload):
        """跑一个用户钩子并落 trace。返回 HookOutcome，绝不抛异常。

        payload 在这里过一次脱敏：钩子是用户的脚本，不是可信执行环境，
        把原始 secret 递给它等于把脱敏边界往外挪了一格。
        """
        self = self.agent
        outcome = hookslib.run_hook(
            self.root,
            point,
            self.redact_artifact(dict(payload or {})),
            env=self.shell_env(),
        )
        if not outcome.ran:
            return outcome
        task_state = getattr(self, "current_task_state", None)
        if task_state is not None:
            event = (
                trace_events.HOOK_DENIED if outcome.denied else trace_events.HOOK_RAN
            )
            self.emit_trace(task_state, event, outcome.to_dict())
        if outcome.error:
            print(f"warning: {point} hook: {outcome.error}", file=sys.stderr)
        self.hook_outcomes.append(outcome.to_dict())
        return outcome

    def delegate_session_store(self):
        self = self.agent
        return SessionStore(str(self.root / ".moss" / "delegates"))

    def delegate_contract(self, goal, args=None):
        """把一次委派请求变成结构化契约。

        `context_seed` 在这里被**显式构造**：当前任务目标 + 相关文件路径。
        刻意不带父 history——截断的对话既不是必要背景也不是完整背景，
        而 sub-agent 作为上下文治理手段的意义正在于此。
        """
        self = self.agent
        args = args or {}
        goal = str(goal).strip()
        seed = []
        task_summary = str(
            self.memory.to_dict().get("working", {}).get("task_summary", "")
        ).strip()
        if task_summary:
            seed.append(f"The parent agent is working on: {task_summary}")
        focus = [
            str(item).strip() for item in args.get("focus") or () if str(item).strip()
        ]
        anchors = [
            str(path) for path in focus or self.relevant_file_anchors(goal) if str(path)
        ][:5]
        if anchors:
            seed.append("Files the parent believes are relevant: " + ", ".join(anchors))
        contract = delegationlib.DelegateContract(
            goal=goal,
            allowed_tools=("list_files", "read_file", "search_text"),
            capabilities=delegationlib.DELEGATE_CAPABILITIES & self.capability_set(),
            max_steps=max(1, int(args.get("max_steps", 3))),
            max_usd=args.get("max_usd"),
            context_seed=tuple(seed),
        )
        contract.validate_against(self.capability_set())
        return contract

    def spawn_delegate(self, args):
        """执行一次委派。`tasks` 给多条时并行 fan-out，结果按提交顺序聚合。"""
        self = self.agent
        args = args or {}
        goals = [
            str(item).strip() for item in args.get("tasks") or () if str(item).strip()
        ]
        if not goals:
            goals = [str(args.get("task", "")).strip()]
        goals = goals[: delegationlib.MAX_FANOUT]
        contracts = [self.delegate_contract(goal, args) for goal in goals]
        if len(contracts) == 1:
            results = [self.run_delegate(contracts[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(contracts)) as pool:
                results = list(pool.map(self.run_delegate, contracts))
        return "\n\n".join((result.render() for result in results))

    def run_delegate(self, contract):
        """跑一个子 agent，返回结构化结果。异常收敛成带 error 的结果，不上抛。"""
        self = self.agent
        task_state = getattr(self, "current_task_state", None)
        if task_state is not None:
            self.emit_trace(
                task_state, trace_events.DELEGATE_SPAWNED, contract.to_dict()
            )
        started_at = time.monotonic()
        try:
            child = type(self)(
                model_client=self.model_client,
                workspace=self.workspace,
                session_store=self.delegate_session_store(),
                run_store=self.run_store,
                approval_policy="never",
                max_steps=contract.max_steps,
                max_new_tokens=self.max_new_tokens,
                depth=self.depth + 1,
                max_depth=self.max_depth,
                read_only=True,
                reflect_mode="off",
                secret_env_names=self.secret_env_names,
                shell_env_allowlist=self.shell_env_allowlist,
                allowed_tools=contract.allowed_tools,
                run_budget_limits={"max_usd": contract.max_usd}
                if contract.max_usd
                else None,
            )
            child.session["memory"]["task"] = contract.goal
            child.session["memory"]["notes"] = [contract.seed_text()]
            answer = child.ask(contract.goal)
            child_state = child.current_task_state
            stop_reason = str(getattr(child_state, "stop_reason", "") or "")
            if stop_reason and stop_reason != STOP_REASON_FINAL_ANSWER_RETURNED:
                result = delegationlib.DelegateResult(
                    goal=contract.goal,
                    error=f"{stop_reason}: {self.redact_text(answer)}",
                )
            else:
                result = delegationlib.parse_delegate_output(
                    answer,
                    goal=contract.goal,
                    verify_anchor=self._delegate_anchor_exists,
                )
            cost = dict(
                child.last_run_budget.snapshot() if child.last_run_budget else {}
            )
            cost["steps"] = child_state.tool_steps if child_state else 0
            cost["wall_s"] = round(time.monotonic() - started_at, 3)
            result = replace(result, cost=cost)
        except Exception as exc:
            result = delegationlib.DelegateResult(
                goal=contract.goal,
                error=self.redact_text(str(exc)),
                cost={"wall_s": round(time.monotonic() - started_at, 3)},
            )
        if task_state is not None:
            self.emit_trace(
                task_state, trace_events.DELEGATE_FINISHED, result.to_dict()
            )
        return result

    def _delegate_anchor_exists(self, raw_path):
        """证据锚点必须真的指向工作区里的一个文件。假锚点比没锚点更糟。"""
        self = self.agent
        try:
            return self.path(raw_path).is_file()
        except Exception:
            return False

    def aux_model_client(self, task_kind="compaction"):
        """给某一类脏活挑后端。没配 aux 就是主后端（行为不变）。

        返回的是绑定了 task_kind 的门面：调用时才决定后端，aux 失败自动回落。
        """
        self = self.agent
        return self.model_router.bind(task_kind)

    def _note_model_route(self, record):
        """把一次路由决定写进 trace。没有活跃 run 时静默丢弃。"""
        self = self.agent
        task_state = getattr(self, "current_task_state", None)
        if task_state is None:
            return None
        return self.emit_trace(task_state, trace_events.MODEL_ROUTED, dict(record))
