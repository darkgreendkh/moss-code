"""多模型路由：便宜模型干脏活（spec-09 §9.7）。

为什么存在：compaction 摘要、失败分类、记忆提炼、judge 都不需要主力模型，
但它们的调用次数可能比主线还多。主线一步、脏活三步，账单是按脏活算的。

三条纪律：

1. **未配置 aux 时行为完全不变**——全部回落主模型，这是消融基线。
2. **aux 的输出不进主线 history**，只进它服务的那个子系统（compaction artifact /
   记忆记录 / 失败标签）。让一个小模型的措辞混进主线推理链是最难查的一类退化。
3. **aux 失败自动回落主模型并记事件**。脏活失败不该让整个 run 停下来，
   但"悄悄换了个模型跑"必须在 report 里看得见（`aux_degraded`）。

路由策略的任何改动都要走配对评测（spec-08 §4.5），不能凭感觉说"省钱了"。
"""

from __future__ import annotations

# 走 aux model 的任务类别。名字进 trace，评测按它切片。
AUX_TASKS = frozenset(
    {"compaction", "reflection", "failure_labeling", "judge", "budget_summary"}
)

ROUTE_MAIN = "main"
ROUTE_AUX = "aux"


class ModelRouter:
    """主线走主模型，脏活走 aux。没配 aux 就全走主模型。"""

    def __init__(self, main_client, aux_client=None, *, observer=None):
        self.main_client = main_client
        self.aux_client = aux_client
        # 每次路由都回调一次，runtime 用它落 `model_routed` trace。
        self.observer = observer
        # 本 run 里 aux 有没有失败过。它进 report，评测里当切片维度用——
        # 一次 aux 失败不算 run 失败，但把两种情况混成一个数就没法解释了。
        self.aux_degraded = False
        self.routes = []

    def _note(self, task_kind, route, client, reason):
        record = {
            "task_kind": str(task_kind),
            "route": route,
            "model": str(getattr(client, "model", "") or ""),
            "provider": str(getattr(client, "provider", "") or ""),
            "reason": reason,
        }
        self.routes.append(record)
        if self.observer is not None:
            try:
                self.observer(record)
            except Exception:
                # 记账不能挡住路由本身，沿用 progress_observer 的纪律。
                pass
        return record

    def route(self, task_kind):
        """给一类任务挑后端。返回的一定是个可用 client，不会是 None。"""
        task_kind = str(task_kind)
        if task_kind not in AUX_TASKS:
            self._note(task_kind, ROUTE_MAIN, self.main_client, "not an aux task")
            return self.main_client
        if self.aux_client is None:
            self._note(task_kind, ROUTE_MAIN, self.main_client, "no aux model configured")
            return self.main_client
        self._note(task_kind, ROUTE_AUX, self.aux_client, "aux task")
        return self.aux_client

    def call(self, task_kind, invoke):
        """跑一次脏活。aux 抛异常就回落主模型，并记一笔降级。

        `invoke(client)` 由调用方给出——路由器不该知道 compaction 和 judge
        各自要怎么调模型，它只负责"用哪个后端"和"失败了怎么办"。
        """
        client = self.route(task_kind)
        try:
            return invoke(client)
        except Exception as exc:
            if client is self.main_client:
                raise
            self.aux_degraded = True
            self._note(task_kind, ROUTE_MAIN, self.main_client, f"aux failed: {exc}")
            return invoke(self.main_client)

    def bind(self, task_kind):
        """给一类脏活返回一个 client 门面。

        为什么要门面：compaction / 反思 这些子系统只认 `complete()`，
        它们不该知道路由存在，更不该各自实现一遍"aux 失败了回落主模型"。
        """
        return RoutedClient(self, task_kind)

    def summary(self):
        """进 report。aux 用了多少次、降级过没有。"""
        aux_calls = sum(1 for record in self.routes if record["route"] == ROUTE_AUX)
        return {
            "aux_configured": self.aux_client is not None,
            "aux_model": str(getattr(self.aux_client, "model", "") or ""),
            "aux_provider": str(getattr(self.aux_client, "provider", "") or ""),
            "aux_calls": aux_calls,
            "aux_degraded": self.aux_degraded,
            "routed_task_kinds": sorted({record["task_kind"] for record in self.routes}),
        }


class RoutedClient:
    """绑定了 task_kind 的 client 门面：调用时才决定后端，失败自动回落。"""

    def __init__(self, router, task_kind):
        self.router = router
        self.task_kind = str(task_kind)

    def _target(self):
        """只用于读身份属性，不记路由——记账发生在真正调用的时候。"""
        if self.task_kind in AUX_TASKS and self.router.aux_client is not None:
            return self.router.aux_client
        return self.router.main_client

    @property
    def provider(self):
        return str(getattr(self._target(), "provider", "") or "")

    @property
    def model(self):
        return str(getattr(self._target(), "model", "") or "")

    @property
    def last_completion_metadata(self):
        return dict(getattr(self._target(), "last_completion_metadata", {}) or {})

    def complete(self, prompt, max_new_tokens, **kwargs):
        return self.router.call(
            self.task_kind, lambda client: client.complete(prompt, max_new_tokens, **kwargs)
        )

    def complete_request(self, request):
        def invoke(client):
            if hasattr(client, "complete_request"):
                return client.complete_request(request)
            return client.complete(request.flatten(), request.max_new_tokens)

        return self.router.call(self.task_kind, invoke)
