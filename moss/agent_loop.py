"""Agent control loop extracted from the runtime facade."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .budget import RunBudget, graceful_final, usage_from_metadata  # noqa: F401
from .clock import now
from .lease import LeaseHeartbeat
from .output_parser import parse_model_actions, truncate_after_final
from .providers.capabilities import probe
from . import trace_events
from .task_state import STATUS_RUNNING, TaskState
from .token_budget import clip, estimate_tokens

# 并发只读工具的上限。固定 4：再多也受限于磁盘和后端延迟，
# 而线程数越多，出问题时越难复现。
PARALLEL_MAX_WORKERS = 4
# 上下文占用率超过这个比例就压缩（spec-06 §4.1）。留 20% 余量是因为
# 压缩本身也要在窗口里完成，等撞墙再压就晚了。
COMPACTION_UTILIZATION_THRESHOLD = 0.8
# 历史段连续被削这么多轮就压缩：一次是偶发的大输出，两次是历史装不下了。
COMPACTION_REDUCTION_STREAK = 2


def _record_instruction_notices(agent, task_state):
    """把就近指令文档作为 runtime notice 追加进 history，并落 trace。

    走 history 而不是 prefix：这是事件性质的（“你刚碰到的目录还有一份规则”），
    append-only，不会让稳定前缀在任务中途改写、白白打掉 prompt 缓存。
    注入不计入 attempts —— 它不是模型的一轮决策。
    """
    for notice in agent.drain_instruction_notices():
        event = notice.get("event")
        if event == trace_events.INSTRUCTION_LOADED:
            agent.record(
                {
                    "role": "system",
                    "content": (
                        f"Runtime notice: {notice['path']} applies to {notice['scope']}/.\n"
                        f"{notice['content']}"
                    ),
                    "created_at": now(),
                }
            )
            agent.emit_trace(task_state, event, {"path": notice["path"], "scope": notice["scope"]})
        elif event == trace_events.INSTRUCTION_CONFLICT:
            agent.emit_trace(
                task_state,
                event,
                {
                    "path": notice["path"],
                    "winner": notice["winner"],
                    "shadowed": notice.get("shadowed", []),
                },
            )


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent
        self._heartbeat = None

    def run(self, user_message):
        run_started_at = time.monotonic()
        self.agent.begin_run()
        try:
            return self._run(user_message, run_started_at)
        except BaseException as exc:
            # 含 KeyboardInterrupt / SystemExit。只做收尾，然后**必然重新抛出**：
            # 语义不变（Ctrl-C 仍然只取消当前轮），但磁盘上不再留下一个
            # 永远停在 running、没有 report 的半截 run 目录。
            self._finish_interrupted(user_message, exc, run_started_at)
            raise
        finally:
            # 本轮结束，用户消息不再是"当前请求"，该以普通历史的身份进入后续轮次。
            # 放在 finally 里：中断退出的会话被 resume 时，历史同样不能缺这一条。
            try:
                self._stop_heartbeat()
                self.agent.clear_pending_history()
            finally:
                self.agent.end_run()

    def _start_heartbeat(self, task_state):
        """开一条后台续租线程，覆盖长模型调用/长工具期间的步间空档。"""
        agent = self.agent
        run_store = getattr(agent, "run_store", None)
        lease = getattr(run_store, "lease", None)
        if lease is None:
            return
        self._heartbeat = LeaseHeartbeat(
            lambda: run_store.heartbeat(task_state.run_id), ttl_s=lease.ttl_s
        ).start()

    def _stop_heartbeat(self):
        heartbeat = getattr(self, "_heartbeat", None)
        if heartbeat is None:
            return
        self._heartbeat = None
        try:
            heartbeat.stop()
        except Exception:
            pass

    def _release_lease(self, task_state):
        """收尾时释放租约。绝不抛异常——它跑在所有收尾路径（含中断）上。"""
        self._stop_heartbeat()
        try:
            self.agent.release_run(task_state)
        except Exception:
            pass

    def _run(self, user_message, run_started_at):
        agent = self.agent
        agent.cancel_token.clear()
        agent.memory.set_task_summary(user_message)
        # 用户消息打 pending：它每一轮都会被 context_manager 渲染成
        # `Current user request`，历史里再来一份就是同一句话说两遍。
        agent.record({"role": "user", "content": user_message, "created_at": now(), "pending": True})

        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_dir = agent.start_run(task_state)
        self._start_heartbeat(task_state)
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        budget = agent.new_run_budget()
        # 进 prompt 的那份请求文本。请求大到装不下时会被换成"摘要 + artifact 指针"，
        # 但 history / checkpoint 里记的始终是用户原话。
        prompt_message = user_message

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < agent.max_steps and attempts < max_attempts:
            # 步边界续租。后台心跳线程覆盖步内的长动作，这一次是同步兜底：
            # 心跳线程起不来（比如受限环境）时，至少每步还有一次刷新。
            agent.heartbeat_run(task_state)
            budget.consume(elapsed_s=time.monotonic() - run_started_at)
            exceeded = budget.hard_exceeded()
            if exceeded:
                # 硬超限：不再调用模型。这时候再发一次请求，
                # 很可能正好把预算捅穿，而收尾本身也需要余量。
                return self._finish_budget_exceeded(task_state, user_message, budget, exceeded, run_started_at)
            attempts += 1
            task_state.record_attempt()
            agent.write_task_state(task_state)
            prompt_bundle = self._build_prompt(task_state, prompt_message)
            # 上下文吃紧时先压缩再重算（spec-06 §4.7 的第 1 步）。
            trigger = self._compaction_trigger(prompt_bundle, budget)
            if trigger and agent.compact_context(trigger=trigger) is not None:
                agent.emit_trace(
                    task_state,
                    trace_events.CONTEXT_COMPACTED,
                    {"trigger": trigger, **agent.session["compactions"][-1]},
                )
                prompt_bundle = self._build_prompt(task_state, prompt_message)
            if not prompt_bundle.sendable and prompt_bundle.overflow_reason == "request_too_large":
                # 第 2 步：当前请求本身就装不下 —— 卸载成 artifact，
                # prompt 里放摘要 + 指针，让模型自己分段读。
                replacement = agent.offload_request(prompt_message)
                if replacement:
                    agent.emit_trace(
                        task_state,
                        trace_events.REQUEST_OFFLOADED,
                        {"chars": len(prompt_message)},
                    )
                    prompt_message = replacement
                    prompt_bundle = self._build_prompt(task_state, prompt_message)
            prompt = prompt_bundle.text
            prompt_metadata = prompt_bundle.metadata
            if not prompt_bundle.sendable:
                # 第 3 步：仍然装不下就不发。发出去的结局是一个 400，
                # 而那时这一轮的钱和时间已经花掉了，用户还只能看到 provider 的报错。
                return self._finish_context_overflow(
                    task_state, user_message, prompt_bundle, run_started_at
                )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            capabilities = getattr(agent.model_client, "capabilities", None)
            supports_cache = bool(
                (capabilities.supports_cache if capabilities is not None else False)
                or getattr(agent.model_client, "supports_prompt_cache", False)
            )
            if agent.feature_enabled("prompt_cache") and supports_cache:
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_request = replace(
                prompt_bundle.request,
                cache_key=prompt_cache_key,
            )
            agent.emit_progress("thinking", {"step": tool_steps + 1, "max_steps": agent.max_steps})
            task_state.record_model_turn()
            model_started_at = time.monotonic()
            try:
                if hasattr(agent.model_client, "complete_request"):
                    raw = agent.model_client.complete_request(model_request)
                else:
                    raw = agent.model_client.complete(
                        prompt,
                        agent.max_new_tokens,
                        prompt_cache_key=prompt_cache_key,
                        prompt_cache_retention=prompt_cache_retention,
                        tools=model_request.tools,
                    )
            except Exception as exc:
                # 模型后端出错（网络中断、超时、5xx）不应该让整个 run 带着一堆
                # 半成品工件崩掉。这里把它收敛成一次「失败但已收尾」的运行：
                # 落 trace、把 task_state 标成 model_error、写 report，再返回一句
                # 人能看懂的话，交给 CLI 展示。KeyboardInterrupt 继承自
                # BaseException 而非 Exception，不会被这里捕获——用户主动取消仍会
                # 正常向上冒泡，由 CLI 决定回到提示符还是退出。
                return self._finish_model_error(task_state, user_message, exc, run_started_at, model_started_at)
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if capabilities is not None:
                detected = probe(agent.model_client, completion_metadata)
                if detected != capabilities:
                    agent.model_client.capabilities = detected
                    agent.model_client.supports_prompt_cache = detected.supports_cache
                    agent.emit_trace(
                        task_state,
                        trace_events.CACHE_CAPABILITY_DETECTED,
                        {"cache_style": detected.cache_style},
                    )
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            input_tokens, output_tokens, estimated = usage_from_metadata(
                completion_metadata, prompt=prompt, completion=raw, measure=estimate_tokens
            )
            if not estimated:
                # 后端报了真实 usage：记一条 (我们估了多少 / 后端说是多少)，
                # 下一轮的预算就按这个比例算。估算出来的数不能当真值用。
                agent.record_token_usage_sample(prompt_metadata.get("prompt_tokens"), input_tokens)
            budget.consume(
                steps=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                elapsed_s=time.monotonic() - run_started_at,
                usd=agent.price_for_usage(input_tokens, output_tokens),
                estimated=estimated,
            )
            agent.last_run_budget = budget
            soft = budget.soft_exceeded()
            if soft:
                agent.emit_trace(task_state, trace_events.BUDGET_SOFT_EXCEEDED, {"dimension": soft})
                agent.record({"role": "system", "content": budget.soft_notice(soft), "created_at": now()})
            actions, dropped = truncate_after_final(
                parse_model_actions(raw, protocol=model_request.protocol)
            )
            kind = actions[0].kind if actions else "retry"
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )
            if dropped:
                # 模型在给出最终答案之后还接着调工具，说明它自己没想清楚。
                # 不执行，只记一笔，等失败分类学攒够数据再决定要不要转成 retry。
                agent.emit_trace(
                    task_state,
                    trace_events.BATCH_TRUNCATED,
                    {"dropped": [action.name or action.kind for action in dropped]},
                )

            tool_actions = [action for action in actions if action.kind == "tool"]
            if tool_actions:
                if model_request.protocol == "native":
                    for action in tool_actions:
                        agent.record(
                            {
                                "role": "assistant",
                                "name": action.name,
                                "args": action.args,
                                "call_id": action.call_id,
                                "native_tool_call": True,
                                "content": "",
                                "created_at": now(),
                            }
                        )
                tool_steps += self._execute_tool_batch(task_state, user_message, tool_actions, tool_steps)

            final_action = next((action for action in actions if action.kind == "final"), None)
            if final_action is None:
                for action in actions:
                    if action.kind == "retry":
                        agent.record({"role": "assistant", "content": action.text, "created_at": now()})
                agent.write_task_state(task_state)
                continue

            if self._needs_verification(task_state):
                # 改了文件却一次验证都没跑过——收尾前拦一次。
                # 只拦一次：模型如果坚持不验证，硬顶着不让它收尾只会烧完预算。
                task_state.verification_requested = True
                attempts += 1
                agent.emit_trace(task_state, trace_events.VERIFICATION_REQUESTED, {})
                agent.record(
                    {
                        "role": "system",
                        "content": (
                            "Runtime notice: this run changed files but never ran a test or lint command. "
                            "Run one now (for example the project's test command), then return your <final> answer. "
                            "If verification is not possible here, say so explicitly in the final answer."
                        ),
                        "created_at": now(),
                    }
                )
                agent.write_task_state(task_state)
                continue

            final = (final_action.text or (raw if isinstance(raw, str) else "")).strip()
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            self._release_lease(task_state)
            return final

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        self._release_lease(task_state)
        return final

    def _build_prompt(self, task_state, user_message):
        """组一轮 prompt 并落 prompt_built。

        压缩之后会再调一次：trace 里必须留下**真正发出去的**那一份，
        否则事后没法解释"模型当时看到的是什么"。
        """
        agent = self.agent
        started_at = time.monotonic()
        bundle = agent.build_context_result(user_message)
        reductions = [entry["section"] for entry in bundle.metadata.get("budget_reductions", [])]
        agent.history_reduction_streak = (
            agent.history_reduction_streak + 1 if "history" in reductions else 0
        )
        agent.emit_trace(
            task_state,
            "prompt_built",
            {
                "prompt_metadata": bundle.metadata,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
        return bundle

    def _compaction_trigger(self, bundle, budget):
        """该不该压缩，以及是因为什么。返回空串表示不压。"""
        agent = self.agent
        if agent.compaction_mode == "off":
            return ""
        if not bundle.sendable:
            return "context_overflow"
        utilization = float(bundle.metadata.get("context_health", {}).get("context_utilization", 0.0))
        if utilization > COMPACTION_UTILIZATION_THRESHOLD:
            return "context_utilization"
        if agent.history_reduction_streak >= COMPACTION_REDUCTION_STREAK:
            # 连续两轮削历史说明不是偶发的大输出，而是历史本身装不下了。
            return "history_reduction"
        if budget.soft_exceeded():
            return "budget_soft_exceeded"
        return ""

    def _can_run_in_parallel(self, actions):
        """只有"全是只读工具、且不止一个"的批才允许并发。

        risky 工具一律降级串行：它们要走审批、要前后各拍一次工作区快照，
        并发执行会让 diff 归属不清——两个写操作同时进行时，谁改了哪个文件
        在快照里根本分不出来。
        """
        agent = self.agent
        if len(actions) < 2 or not getattr(agent, "parallel_tools", False):
            return False
        for action in actions:
            tool = agent.tools.get(action.name)
            if tool is None or tool.get("risky", True):
                return False
        return True

    def _execute_tool_batch(self, task_state, user_message, actions, tool_steps_so_far):
        """执行一批工具动作，返回本批消耗的 tool_steps。

        顺序不变量：无论是否并发，写回 history / trace 的顺序恒等于
        `Action.index` 的顺序，不是完成顺序。这条是录制回放能成立的前提。
        并发阶段只做纯执行，memory 写入 / record / trace 全部回主线程按序补做。
        """
        agent = self.agent
        budget_left = max(0, agent.max_steps - tool_steps_so_far)
        actions = sorted(actions, key=lambda action: action.index)[:budget_left]
        if not actions:
            return 0

        parallel = self._can_run_in_parallel(actions)
        agent.emit_trace(
            task_state,
            trace_events.TOOLS_BATCH_STARTED,
            {"count": len(actions), "parallel": parallel},
        )
        batch_started_at = time.monotonic()

        for action in actions:
            task_state.record_tool_call(action.name)
            agent.emit_progress("tool", {"name": action.name, "args": action.args})

        results = self._run_actions(actions, parallel)

        for action, (result, duration_ms) in zip(actions, results):
            task_state.record_tool(action.name)
            content = result.content
            metadata = dict(result.metadata or {})
            # 并发阶段被推迟的副作用在这里按 index 顺序补做。
            agent.update_memory_after_tool(action.name, action.args, content)
            agent.record_process_note_for_tool(action.name, metadata)
            agent.record_tool_outcome(action.name, action.args, metadata)
            agent.emit_progress(
                "tool_result",
                {
                    "name": action.name,
                    "status": metadata.get("tool_status", "ok"),
                    "duration_ms": duration_ms,
                },
            )
            entry = {
                "role": "tool",
                "name": action.name,
                "args": action.args,
                "content": content,
                "created_at": now(),
            }
            if action.call_id:
                entry["call_id"] = action.call_id
            if metadata.get("artifact_path"):
                # 指针跟着历史条目走：模型下一轮看到的摘要必须带上"完整输出在哪"，
                # 否则"可以取回"只是一句空话。
                entry["artifact"] = metadata["artifact_path"]
                entry["artifact_lines"] = int(metadata.get("artifact_lines", 0) or 0)
            agent.record(entry)
            if action.name in ("read_file", "write_file", "edit_file"):
                anchor_miss = agent.note_anchor_outcome((action.args or {}).get("path", ""))
                if anchor_miss:
                    agent.emit_trace(task_state, trace_events.ANCHOR_MISS, anchor_miss)
            _record_instruction_notices(agent, task_state)
            agent.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "tool_executed",
                {
                    "name": action.name,
                    "args": action.args,
                    "result": clip(content, 500),
                    "duration_ms": duration_ms,
                    **metadata,
                },
            )
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
            agent.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "tool_executed"},
            )

        agent.emit_trace(
            task_state,
            trace_events.TOOLS_BATCH_FINISHED,
            {
                "count": len(actions),
                "duration_ms": int((time.monotonic() - batch_started_at) * 1000),
            },
        )
        self._check_stall(task_state)
        self._check_plan_pressure(task_state)
        return len(actions)

    def _check_plan_pressure(self, task_state):
        """某一步吃掉的步数远超均摊预算时，建议模型重规划。

        软提示而不是硬拦截：计划本来就会变，真正的问题是"卡在同一步却不承认"。
        """
        agent = self.agent
        pressure = agent.check_plan_pressure()
        if not pressure:
            return
        agent.emit_trace(task_state, trace_events.PLAN_PRESSURE, pressure)
        agent.record(
            {
                "role": "system",
                "content": (
                    f"Runtime notice: plan step {pressure['step_id']} ({pressure['title']}) has taken "
                    f"{pressure['spent_steps']} tool steps. Update the plan with update_plan if the approach changed."
                ),
                "created_at": now(),
            }
        )

    def _needs_verification(self, task_state):
        """改了文件却一次验证都没跑过 —— 收尾前拦一次。

        为什么值得多花一轮：未经验证的改动是 agent 最常见的"看起来完成了"，
        而它恰恰最贵——用户要么自己发现，要么在下一次运行里当成既有事实。
        """
        agent = self.agent
        if not getattr(agent, "verify_before_final", True) or task_state.verification_requested:
            return False
        if "run_shell" not in getattr(agent, "tools", {}):
            # 这次运行根本没有能跑验证的工具（比如 --allowed-tools 只给了读写）。
            # 要求它去验证只会白白烧掉一轮，模型也做不到。
            return False
        changed = False
        verified = False
        for outcome in agent.stall_events():
            if outcome.get("workspace_changed"):
                changed = True
            if outcome.get("verification"):
                verified = True
        return changed and not verified

    def _check_stall(self, task_state):
        """每批结束后查一次停滞，命中就注入结构化干预。

        注入的不是"拒绝执行"，而是"你在重复什么 / 已知失败原因 / 可以怎么换路"——
        单纯拒绝只会让模型换个参数再试一次，浪费的还是同一份预算。
        同一类信号只干预一次：重复喊同一句话既费 token 又没有新信息。
        """
        agent = self.agent
        signal = agent.detect_stall()
        if signal is None or signal.kind in agent.stall_notices_sent:
            return
        agent.stall_notices_sent.add(signal.kind)
        agent.emit_trace(
            task_state,
            trace_events.STALL_DETECTED,
            {"kind": signal.kind, "detail": signal.detail, "window": signal.window},
        )
        agent.record({"role": "system", "content": signal.notice(), "created_at": now()})

    def _run_actions(self, actions, parallel):
        """纯执行阶段：返回和 actions 等长、同序的 [(结果, 耗时ms)]。"""
        agent = self.agent

        def run_one(action):
            started_at = time.monotonic()
            result = agent.execute_tool(action.name, action.args, defer_side_effects=parallel)
            return result, int((time.monotonic() - started_at) * 1000)

        if not parallel:
            return [run_one(action) for action in actions]

        with ThreadPoolExecutor(max_workers=min(PARALLEL_MAX_WORKERS, len(actions))) as pool:
            # map 保序：即使后提交的先跑完，结果也按提交顺序返回。
            return list(pool.map(run_one, actions))

    def _finish_budget_exceeded(self, task_state, user_message, budget, dimension, run_started_at):
        """预算耗尽时的优雅收尾：规则生成总结，不再花模型调用。"""
        agent = self.agent
        final = graceful_final(task_state, dimension)
        agent.emit_trace(
            task_state,
            trace_events.BUDGET_EXCEEDED,
            {"dimension": dimension, **budget.snapshot()},
        )
        task_state.stop_budget_exceeded(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="budget_exceeded")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "budget_exceeded"},
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        self._release_lease(task_state)
        return final

    def _finish_context_overflow(self, task_state, user_message, result, run_started_at):
        """prompt 装不进可用预算：不调用 provider，直接收尾成一次失败运行。

        为什么算失败而不是"停下"：请求根本没被处理过，one-shot / CI 必须能靠
        退出码区分它和"跑完了但没成功"。
        """
        agent = self.agent
        reason = result.overflow_reason or "prompt_too_large"
        detail = str(result.metadata.get("overflow_detail", "")) or reason
        final = f"Stopped without calling the model: {detail}."
        agent.emit_progress("error", {"scope": "context", "message": detail})
        agent.emit_trace(
            task_state,
            trace_events.CONTEXT_OVERFLOW,
            {
                "reason": reason,
                "detail": detail,
                "prompt_measured": result.metadata.get("prompt_measured"),
                "prompt_budget": result.metadata.get("prompt_budget_chars"),
            },
        )
        task_state.stop_context_overflow(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_overflow")
        agent.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "context_overflow"},
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        self._release_lease(task_state)
        return final

    def _finish_interrupted(self, user_message, exc, run_started_at):
        """把一次中断收敛成一次已收尾的失败运行。

        为什么单独一条路径：`_finish_model_error` 只覆盖模型后端异常，
        用户 Ctrl-C、SIGTERM、以及工具里冒出来的意外异常都不走那里，
        结果是 run 目录停在 running 且没有 report——恢复流程看到的是一个
        无法判断"跑到哪了"的空壳。

        这个函数自己必须绝不抛异常：它跑在 except 里，再抛就会把原始异常
        （用户真正想看到的那个）替换掉。
        """
        agent = self.agent
        task_state = getattr(agent, "current_task_state", None)
        if task_state is None or task_state.status != STATUS_RUNNING:
            # 还没开始、或已经正常收尾（比如 finally 之后的异常），没什么可补的。
            return
        try:
            agent.cancel_token.set()
            reason = exc.__class__.__name__
            final = f"Run interrupted before completion ({reason})."
            agent.emit_trace(
                task_state,
                trace_events.RUN_INTERRUPTED,
                {"reason": reason, "detail": agent.redact_text(str(exc))[:300]},
            )
            task_state.stop_interrupted(final)
            agent.write_task_state(task_state)
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="interrupted")
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "interrupted"},
            )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            self._release_lease(task_state)
        except Exception:
            # 收尾本身失败也不能盖掉原始异常。工件不全总好过丢掉中断原因。
            pass

    def _finish_model_error(self, task_state, user_message, exc, run_started_at, model_started_at):
        """把一次模型后端失败收敛成一次已收尾的失败运行。

        这样即使后端挂了，磁盘上留下的仍然是一份完整、可复盘的运行工件
        （task_state=failed / stop_reason=model_error + report），而不是一个
        永远停在 running、没有 report 的半截目录。
        """
        agent = self.agent
        message = str(exc).strip() or exc.__class__.__name__
        # 后端错误信息里可能带 URL/key 之类，统一走脱敏再落盘和展示。
        safe_message = agent.redact_text(message)
        final = f"Model backend error, stopped without a final answer: {safe_message}"
        agent.emit_progress("error", {"scope": "model", "message": safe_message})
        agent.emit_trace(
            task_state,
            "model_error",
            {
                "error": safe_message,
                "error_type": exc.__class__.__name__,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
            },
        )
        task_state.stop_model_error(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="model_error")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": "model_error",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        self._release_lease(task_state)
        return final
