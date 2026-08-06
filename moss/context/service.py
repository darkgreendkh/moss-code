"""Context, memory and repository operations composed behind Moss."""

import os
from dataclasses import replace
from pathlib import Path

from moss.clock import now
from moss.context import compaction as compactionlib
from moss.context.model_request import PromptBundle
from moss.context.prefix import build_prompt_prefix
from moss.context.repository import ignore as ignorelib
from moss.context.repository import repo_map as repo_maplib
from moss.context.repository.workspace import (
    WorkspaceContext,
    capture_snapshot,
    diff_snapshots,
    find_nearest_instruction_docs,
)
from moss.context.token_budget import (
    MAX_HISTORY,
    calibrated_measure,
    clip,
    estimate_tokens,
    exact_token_counter,
)
from moss.execution.safety import secrets as securitylib
from moss.memory import service as memorylib
from moss.memory.records import SourceRef
from moss.runs import checkpoint as checkpointlib
from moss.runs.checkpoint import CHECKPOINT_NONE_STATUS
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


class ContextService:
    def __init__(self, agent):
        self.agent = agent

    def build_prefix(self):
        self = self.agent
        return build_prompt_prefix(
            workspace=self.workspace,
            tools=self.tools,
            skills=self.skills,
            protocol=self.resolved_tool_protocol(),
            catalog_threshold=self.tool_catalog_threshold,
        )

    def _apply_prefix_state(self, prefix_state):
        self = self.agent
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        self = self.agent
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(
            getattr(self, "prefix_state", None), "workspace_fingerprint", None
        )
        refreshed_workspace = WorkspaceContext.build(
            self.invocation_cwd, repo_root_override=self.root
        )
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = (
            refreshed_workspace_fingerprint != previous_workspace_fingerprint
        )
        if (workspace_changed or force) and (not self._run_active):
            self.attach_repo_map(refreshed_workspace)
            self.workspace = refreshed_workspace
        registry_drift = self._detect_registry_drift()
        if not self._run_active:
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
                    self.emit_trace(
                        self.current_task_state,
                        trace_events.TOOL_REGISTRY_DRIFT,
                        registry_drift,
                    )
        prefix_state = self.build_prefix()
        prefix_changed = (
            force or previous_hash is None or previous_hash != prefix_state.hash
        )
        if prefix_changed:
            self._apply_prefix_state(prefix_state)
        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
            "registry_drift": bool(registry_drift),
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        self = self.agent
        return self.memory.render_memory_text()

    def history_text(self):
        self = self.agent
        return clip(self.context_manager.render_history_text(), MAX_HISTORY)

    def feature_enabled(self, name):
        self = self.agent
        return bool(self.feature_flags.get(str(name), False))

    def token_measure(self):
        """这一轮预算用的计量函数：真值优先，其次校准过的估算。

        探测到 tiktoken 就直接用真值（ratio 强制 1.0）；否则用
        `estimate_tokens * ratio`，ratio 来自最近 50 条"我估了多少 / 后端说是多少"。
        """
        self = self.agent
        exact = exact_token_counter(getattr(self.model_client, "model", ""))
        if exact is not None:
            self.token_calibration = replace(
                self.token_calibration, ratio=1.0, exact=True
            )
            return exact
        return calibrated_measure(estimate_tokens, self.token_calibration)

    def record_token_usage_sample(self, estimated_tokens, actual_tokens):
        """记一条 (估算, 后端真值) 样本，并把校准结果用到下一轮预算上。

        只在后端**真的报了** usage 时才记：拿我们自己的估算当"真值"去校准
        我们自己的估算，只会把偏差固化下来。
        """
        self = self.agent
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
        self = self.agent
        (prompt, _) = self._build_prompt_and_metadata(user_message)
        return prompt

    def clear_pending_history(self):
        """去掉历史里的 pending 标记。

        pending 的含义是"这条内容已经在本轮 prompt 的别处出现过了"，
        只对当前这一次运行成立；运行结束后它就是普通历史。
        """
        self = self.agent
        changed = False
        for item in self.session.get("history", []):
            if item.pop("pending", None):
                changed = True
        if changed:
            self.session_path = self.session_store.save(self.session)

    def record(self, item):
        self = self.agent
        safe_item = self.redact_artifact(dict(item or {}))
        self.session["history"].append(safe_item)
        self.session_path = self.session_store.save(self.session)

    def is_secret_env_name(self, name):
        self = self.agent
        return securitylib.is_secret_env_name(
            name, secret_env_names=self.secret_env_names
        )

    def configured_secret_env_items(self):
        self = self.agent
        return securitylib.configured_secret_env_items(
            secret_env_names=self.secret_env_names
        )

    def detected_secret_env_items(self):
        self = self.agent
        return securitylib.detected_secret_env_items(
            secret_env_names=self.secret_env_names
        )

    def secret_env_summary(self):
        self = self.agent
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        self = self.agent
        return securitylib.detected_secret_env_summary(
            secret_env_names=self.secret_env_names
        )

    def redact_text(self, text):
        self = self.agent
        return securitylib.redact_text(text, secret_env_names=self.secret_env_names)

    def redact_artifact(self, value, key=None):
        self = self.agent
        return securitylib.redact_artifact(
            value, key=key, secret_env_names=self.secret_env_names
        )

    def shell_env(self):
        self = self.agent
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def prompt_metadata(self, user_message, prompt):
        self = self.agent
        (_, metadata) = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        self = self.agent
        bundle = self._build_prompt_bundle_and_metadata(user_message)
        return (bundle.text, bundle.metadata)

    def _build_prompt_bundle_and_metadata(self, user_message):
        self = self.agent
        result = self.build_context_result(user_message)
        return PromptBundle(
            request=result.request, text=result.text, metadata=result.metadata
        )

    def build_context_result(self, user_message):
        """组这一轮的 prompt，并带上 admission 判定（能不能发）。"""
        self = self.agent
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        bundle = self.context_manager.build_result(user_message)
        metadata = dict(bundle.metadata)
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
                "prompt_cache_key": self.prefix_state.stable_hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "skill_signature": self.prefix_state.skill_signature,
                "prompt_version": self.prefix_state.prompt_version,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(
                    getattr(self.model_client, "supports_prompt_cache", False)
                ),
                "resume_status": self.resume_state.get(
                    "status", CHECKPOINT_NONE_STATUS
                ),
                "stale_summary_invalidations": int(
                    self.resume_state.get("stale_summary_invalidations", 0)
                ),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(
                    self.resume_state.get("runtime_identity_mismatch_fields", [])
                ),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return replace(bundle, metadata=metadata)

    def record_memory_event(self, event, payload=None):
        self = self.agent
        task_state = getattr(self, "current_task_state", None)
        if task_state is None:
            return None
        return self.emit_trace(task_state, event, payload)

    def memory_source_refs(self):
        self = self.agent
        task_state = getattr(self, "current_task_state", None)
        run_id = (
            task_state.run_id
            if task_state is not None
            else f"session:{self.session['id']}"
        )
        return (SourceRef(run_id=run_id),)

    def memory_write_action(self, args):
        self = self.agent
        (record, reason) = self.memory.write_durable(
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
        return {
            "status": "written",
            "id": record.id,
            "scope": record.scope,
            "trust": record.trust,
        }

    def memory_update_action(self, args):
        self = self.agent
        (record, reason) = self.memory.update_durable(
            args.get("id", ""),
            args.get("text", ""),
            trust="model",
            source_refs=self.memory_source_refs(),
        )
        if record is None:
            return self._memory_rejection(reason)
        self._save_memory_state()
        return {
            "status": "updated",
            "id": record.id,
            "supersedes": list(record.supersedes),
        }

    def memory_delete_action(self, args):
        self = self.agent
        (record, reason) = self.memory.delete_durable(args.get("id", ""))
        if record is None:
            return self._memory_rejection(reason)
        self._save_memory_state()
        return {"status": "deleted", "deleted": record.id}

    def memory_search_action(self, args):
        self = self.agent
        return self.memory.search(
            args.get("query", ""), limit=int(args.get("limit", 5))
        )

    def _save_memory_state(self):
        self = self.agent
        self.session["memory"] = self.memory.to_dict()
        self.session_path = self.session_store.save(self.session)

    def attach_repo_map(self, workspace):
        """把仓库地图挂到 workspace 段。

        为什么在这里而不是 WorkspaceContext.build 里：地图要用 agent 的忽略口径
        和 .moss/cache 目录，而 workspace 构建是个不认识 agent 的纯函数。
        MOSS_REPO_MAP=off 是一键回退开关——关掉后 prefix 与加地图前逐字节一致。
        """
        self = self.agent
        if str(os.environ.get("MOSS_REPO_MAP", "on")).strip().lower() in {
            "off",
            "0",
            "false",
            "no",
        }:
            workspace.repo_map_text = ""
            self.last_repo_map = None
            return workspace
        try:
            budget = int(
                os.environ.get(
                    "MOSS_REPO_MAP_BUDGET", repo_maplib.DEFAULT_BUDGET_TOKENS
                )
            )
        except ValueError:
            budget = repo_maplib.DEFAULT_BUDGET_TOKENS
        try:
            repo_map = repo_maplib.get_repo_map(
                self.root, ignore=self.workspace_ignore_rules(), budget_tokens=budget
            )
            workspace.repo_map_text = repo_maplib.render_repo_map(repo_map, budget)
            self.last_repo_map = repo_map
        except Exception:
            workspace.repo_map_text = ""
            self.last_repo_map = None
        return workspace

    def relevant_file_anchors(self, user_message, limit=5):
        """给当前请求算一组“最可能相关的文件”，作为模型的起点锚。

        进的是 relevant_memory 段而不是 prefix：它随每轮请求变化，
        放进稳定段会让 prompt 缓存每轮失效。
        """
        self = self.agent
        if self.last_repo_map is None:
            self.last_relevant_anchors = []
            return []
        try:
            anchors = repo_maplib.rank_relevant_files(
                self.last_repo_map, user_message, limit=limit
            )
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
        self = self.agent
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
        self = self.agent
        try:
            found = find_nearest_instruction_docs(self.root, self.root / str(path))
        except Exception:
            return []
        if not found:
            return []
        nearest = found[0]
        if len(found) > 1:
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
        self = self.agent
        notices = list(self.pending_instruction_notices)
        self.pending_instruction_notices.clear()
        return notices

    def workspace_ignore_rules(self):
        """快照/地图共用的一套忽略口径，按 root 缓存。

        每次 risky 工具都重新解析 .gitignore 是纯浪费；而两处口径不一致会让
        模型看到的目录树和 diff 里的路径对不上。
        """
        self = self.agent
        if self._ignore_rules is None:
            self._ignore_rules = ignorelib.IgnoreRules.load(
                self.root,
                extra_patterns=ignorelib.parse_exclude_globs(
                    os.environ.get("MOSS_SNAPSHOT_EXCLUDE", "")
                ),
            )
        return self._ignore_rules

    def capture_workspace_snapshot(self):
        self = self.agent
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
        self = self.agent
        return diff_snapshots(before, after, untracked=self._last_snapshot.untracked)

    def snapshot_strategy(self):
        """最近一次快照实际走的策略，进 report 供评测口径识别降级情况。"""
        self = self.agent
        return self._last_snapshot.strategy

    def infer_next_step(self, task_state):
        self = self.agent
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        self = self.agent
        return memorylib.update_memory_after_tool(self, name, args, result)

    def record_process_note_for_tool(self, name, metadata):
        self = self.agent
        return memorylib.record_process_note_for_tool(self, name, metadata)

    def promote_durable_memory(self, user_message, final_answer):
        self = self.agent
        self.memory.mark_used(final_answer)
        (promotions, rejections) = memorylib.extract_durable_promotions(
            user_message, final_answer, redact_text=self.redact_text
        )
        task_state = getattr(self, "current_task_state", None)
        source_refs = ()
        if task_state is not None:
            source_refs = (SourceRef(run_id=task_state.run_id),)
        (promoted, superseded) = self.memory.promote_durable(
            promotions, source_refs=source_refs, trust="user"
        )
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = promoted
        self.last_durable_rejections = rejections
        self.last_durable_superseded = superseded
        self.last_procedural_distilled = self.distill_current_run()
        return (promoted, rejections, superseded)

    def _reflection_summarizer(self):
        """反思提炼的模型侧改写。走 aux 路由；出错就返回空串退回规则结果。

        为什么吞异常：提炼是收尾阶段的锦上添花，让它把一次成功的 run
        变成失败的 run 完全不划算。
        """
        self = self.agent
        if self._aux_model_client is None:
            return None
        client = self.aux_model_client("reflection")

        def summarize(text, max_tokens=200):
            try:
                return client.complete(
                    "Rewrite this lesson as one short reusable sentence. Keep it factual, no preamble.\n\n"
                    + str(text),
                    int(max_tokens),
                )
            except Exception:
                return ""

        return summarize

    def distill_current_run(self):
        self = self.agent
        if self.reflect_mode == "off" or self.current_task_state is None:
            return []
        records = memorylib.distill_run(
            self.run_store.read_trace(self.current_task_state.run_id),
            mode=self.reflect_mode,
            workspace_root=self.root,
            model_summarizer=self._reflection_summarizer()
            if self.reflect_mode == "model"
            else None,
        )
        stored = [
            self.memory.durable_store.store.append_procedural(record)
            for record in records
        ]
        self.session["memory"] = self.memory.to_dict()
        return [record.id for record in stored]

    def compact_context(self, trigger="context_pressure"):
        """把较早的历史压成一份结构化交接，返回 artifact（没压则返回 None）。

        在链路里的位置：主循环发现上下文吃紧（占用率过高 / 历史连续被削 /
        软预算命中 / prompt 根本发不出去）时调用它，然后重新组一次 prompt。
        """
        self = self.agent
        if self.compaction_mode == "off":
            return None
        task_state = self.current_task_state
        if task_state is None:
            return None
        history = list(self.session.get("history", []))
        pending = [item for item in history if item.get("pending")]
        compactable = [item for item in history if not item.get("pending")]
        (artifact, remaining) = compactionlib.compact(
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
        if all((item.get("compaction") for item in covered)):
            return None
        compactions = self.session.setdefault("compactions", [])
        (raw_path, _) = self.run_store.write_context_turns(
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
        self.session["history"] = [summary_entry, *pending, *remaining]
        compactions.append(artifact.to_dict())
        self.session_path = self.session_store.save(self.session)
        self.history_reduction_streak = 0
        return artifact
