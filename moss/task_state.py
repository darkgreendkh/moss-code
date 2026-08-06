"""一次 ask() 运行过程中的状态机快照。

它回答的是：这次用户请求当前进行到哪了、调了多少次工具、最后为什么停下。
这个对象会被不断写入 task_state.json，供运行中观察和运行后复盘。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_STEP_LIMIT_REACHED = "step_limit_reached"
STOP_REASON_RETRY_LIMIT_REACHED = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_DELEGATE_FAILED = "delegate_failed"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"
STOP_REASON_INTERRUPTED = "interrupted"
STOP_REASON_BUDGET_EXCEEDED = "budget_exceeded"
STOP_REASON_CONTEXT_OVERFLOW = "context_overflow"


@dataclass
class TaskState:
    run_id: str
    task_id: str
    user_request: str
    status: str = STATUS_RUNNING
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""
    checkpoint_id: str = ""
    resume_status: str = ""
    # 记账字段（spec-02 §4.2）。tool_steps 的语义完全不动，这里只增不改：
    # model_turns 是"模型被调用了几轮"（成本的主口径），
    # tool_calls 是"工具被调用了几次，含失败的"（用来算失败率/无效调用率）。
    # 一轮模型输出可能带多个工具调用，所以 tool_calls >= tool_steps。
    model_turns: int = 0
    tool_calls: int = 0
    verification_requested: bool = False
    # 显式计划（update_plan 写入）。跑偏时能对照它看出偏在哪一步。
    plan: list = None

    @classmethod
    def create(cls, task_id, user_request, run_id=""):
        if not run_id:
            run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(run_id=run_id, task_id=task_id, user_request=user_request)

    @classmethod
    def from_dict(cls, data):
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            user_request=str(data.get("user_request", "")),
            status=str(data.get("status", STATUS_RUNNING)),
            tool_steps=int(data.get("tool_steps", 0)),
            attempts=int(data.get("attempts", 0)),
            last_tool=str(data.get("last_tool", "")),
            stop_reason=str(data.get("stop_reason", "")),
            final_answer=str(data.get("final_answer", "")),
            checkpoint_id=str(data.get("checkpoint_id", "")),
            resume_status=str(data.get("resume_status", "")),
            model_turns=int(data.get("model_turns", 0)),
            tool_calls=int(data.get("tool_calls", 0)),
            verification_requested=bool(data.get("verification_requested", False)),
            plan=list(data.get("plan", []) or []),
        )

    def record_attempt(self):
        # attempt 统计的是“模型被调用了几轮”，不等于 tool_steps。
        self.attempts += 1
        return self

    def record_model_turn(self):
        # 模型调用轮数。attempts 会因为重试等原因和它不一致，
        # 成本核算要的是"发了几次请求"，所以单独记一份。
        self.model_turns += 1
        return self

    def record_tool_call(self, name):
        # 工具调用总数，含被拒绝/校验失败的那些。失败率的分母。
        self.tool_calls += 1
        self.last_tool = str(name or "")
        return self

    def record_tool(self, name):
        # tool_steps 只统计真正进入执行阶段的工具调用次数。
        self.tool_steps += 1
        self.last_tool = str(name or "")
        return self

    def stop(self, stop_reason, status=STATUS_STOPPED, final_answer=""):
        # stop_reason 和 status 分开存，是为了区分“怎么停的”和“停下时是什么状态”。
        self.status = status
        self.stop_reason = stop_reason
        if final_answer != "":
            self.final_answer = final_answer
        return self

    def stop_step_limit(self, final_answer=""):
        return self.stop(STOP_REASON_STEP_LIMIT_REACHED, final_answer=final_answer)

    def stop_retry_limit(self, final_answer=""):
        return self.stop(STOP_REASON_RETRY_LIMIT_REACHED, final_answer=final_answer)

    def stop_model_error(self, final_answer=""):
        return self.stop(STOP_REASON_MODEL_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def stop_budget_exceeded(self, final_answer=""):
        # 预算耗尽是"按计划停下"，不是失败：status 保持 stopped。
        return self.stop(STOP_REASON_BUDGET_EXCEEDED, final_answer=final_answer)

    def stop_context_overflow(self, final_answer=""):
        # 装不进上下文是"这次请求发不出去"，属于失败：one-shot 要靠非零退出码
        # 让 CI 看见，绝不能伪装成一次正常收尾。
        return self.stop(STOP_REASON_CONTEXT_OVERFLOW, status=STATUS_FAILED, final_answer=final_answer)

    def stop_interrupted(self, final_answer=""):
        return self.stop(STOP_REASON_INTERRUPTED, status=STATUS_FAILED, final_answer=final_answer)

    def finish_success(self, final_answer):
        self.status = STATUS_COMPLETED
        self.stop_reason = STOP_REASON_FINAL_ANSWER_RETURNED
        self.final_answer = str(final_answer)
        return self

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
            "model_turns": self.model_turns,
            "tool_calls": self.tool_calls,
            "verification_requested": self.verification_requested,
            "plan": list(self.plan or []),
        }
