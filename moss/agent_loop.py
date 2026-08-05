"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .clock import now
from .output_parser import parse_model_output
from . import trace_events
from .task_state import STATUS_RUNNING, TaskState
from .token_budget import clip


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

    def run(self, user_message):
        run_started_at = time.monotonic()
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
            self.agent.clear_pending_history()

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

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < agent.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            agent.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
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
            if getattr(agent.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            native_tools = None
            if getattr(agent.model_client, "supports_native_tools", False):
                native_tools = agent.native_tool_definitions()
            agent.emit_progress("thinking", {"step": tool_steps + 1, "max_steps": agent.max_steps})
            model_started_at = time.monotonic()
            try:
                raw = agent.model_client.complete(
                    prompt,
                    agent.max_new_tokens,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                    tools=native_tools,
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
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            kind, payload = parse_model_output(raw)
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)
                agent.emit_progress("tool", {"name": name, "args": args})
                tool_started_at = time.monotonic()
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                agent.emit_progress(
                    "tool_result",
                    {
                        "name": name,
                        "status": (tool_result.metadata or {}).get("tool_status", "ok"),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                    },
                )
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )
                if name in ("read_file", "write_file", "edit_file"):
                    anchor_miss = agent.note_anchor_outcome(args.get("path", ""))
                    if anchor_miss:
                        agent.emit_trace(task_state, trace_events.ANCHOR_MISS, anchor_miss)
                _record_instruction_notices(agent, task_state)
                agent.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
                agent.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                continue

            if kind == "retry":
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.write_task_state(task_state)
                continue

            final = (payload or raw).strip()
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
        return final
