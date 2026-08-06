# 主循环（agent loop）

> 代码：`moss/agent_loop.py` · `moss/task_state.py` · `moss/output_parser.py` ·
> `moss/stall.py` · `moss/budget.py` · `moss/verification.py`
> 设计稿：[spec-02](../specs/spec-02-agent-loop.md)

主循环是 moss 的心脏。它每一轮做四件事——**感知、决策、行动、记录**——
然后判断该不该停。整个模块的设计围绕一句话展开：
**任何一条退出路径都必须留下完整、可审计、可恢复的工件**。

---

## 1. 一步的形状

```python
while True:
    heartbeat()                      # 步边界续租
    if budget.hard_exceeded():       # 硬超限：不再调模型
        return graceful_final()
    bundle = context_manager.build() # 感知
    if not bundle.sendable:          # admission gate
        return finish_context_overflow()
    reply = model.complete(...)      # 决策
    kind, payload = parse(reply)     # 解析
    if kind == "final":
        if needs_verification(): ...  # 收尾自检，只拦一次
        fire_pre_final_hook()         # 纯观察，无否决权
        return payload
    execute_tool_batch(payload)       # 行动
    record(...)                       # 记录
```

`agent_loop.py:161` 附近的注释把这四拍写在代码里，因为它是理解全文件的钥匙。

### 感知

`ContextManager` 每轮**重新组**一份 prompt（默认 `--context-mode=rerender`）。
不是"往历史后面追加"，因为历史需要按预算裁剪、压缩、卸载，
而这些操作会改变前面已经渲染过的内容。`append_only` 模式保留稳定消息序列，
换取更高的 cache 命中率，代价是上下文治理的手段变少。

### 决策

provider 调用统一走 `providers/clients.py::complete()`。
一次调用可能返回多个动作——`parse_model_actions` 按**出现位置**解析多个 `<tool>` 块。

### 行动

见下面第 3 节。

### 记录

每步都写：history 条目、`task_state.json`、一条 trace 事件、可能的 memory 更新、
一个 checkpoint。checkpoint 上限 `CHECKPOINT_HISTORY_LIMIT = 40`，超了自动裁剪。

---

## 2. 用户请求为什么不进 history

用户消息在本轮被标成 `pending`：它每一轮都会被 `ContextManager` 渲染成
`Current user request` 段。如果同时又作为普通历史条目存在，
首轮 prompt 里同一句话会出现两次——浪费 token，而且"哪个才是当前请求"变得模糊。

本轮结束后（在 `finally` 里）它才以普通历史条目的身份沉下去。
放在 `finally` 是因为中断退出的会话被 resume 时，历史同样不能缺这一条。

进 prompt 的那份请求文本在超预算时可能被换成"摘要 + artifact 指针"，
但 **history / checkpoint 里记的始终是用户原话**。

---

## 3. 一轮多个动作

`parse_model_actions` 返回一个动作列表。三条规则：

1. **`final` 之后的动作一律丢弃**，记 `batch_truncated`。
   模型在给出最终答案后还继续调工具，说明它自己没想清楚；
   执行这些动作等于让一个已经"交卷"的决策继续产生副作用。
2. **顺序不变量**：写回 history/trace 的顺序恒等于 `Action.index`，
   不是完成顺序。录制回放依赖这条——顺序一变，请求指纹就不一样了。
3. **只读工具批可并发**（`--parallel-tools`，默认 off，上限 4 线程）。
   并发阶段只做纯执行；memory 更新、artifact 落盘、trace 记录
   全部回主线程按 index 顺序补做。risky 工具恒串行。

并发上限固定 4：再多也受限于磁盘和后端延迟，而线程数越多，出问题时越难复现。

---

## 4. 停机路径

`task_state.stop_reason` 是一次 run 为什么结束的唯一口径。
每条路径都有专门的收尾函数，它们**自己绝不抛异常**。

| stop_reason | 触发 | 收尾动作 |
| --- | --- | --- |
| （正常 final） | 模型给出最终答案且通过自检 | 写 report、释放租约、跑 `post_run` |
| `budget_exceeded` | 步数/token/时间/金额任一硬阈值 | `graceful_final()` 收敛成一句话，不再调模型 |
| `context_overflow` | admission gate 判定装不下 | **不调用 provider**，失败运行，one-shot 非零退出 |
| `model_error` | 后端异常（网络/超时/5xx） | 收敛为已收尾的失败运行，错误信息脱敏 |
| `interrupted` | `BaseException`（含 Ctrl-C） | 收尾 + 释放租约，然后**必然重新抛出** |
| `tool_timeout` | 工具执行超时 | 按普通工具失败处理，交回模型 |

三条设计取舍值得单独说：

- **硬超限不再调模型**。这时候再发一次请求很可能正好把预算捅穿，
  而收尾本身也需要余量。`graceful_final` 用已有信息拼一个诚实的收尾答案。
- **`KeyboardInterrupt` 继承 `BaseException`**，不会被 `except Exception` 捕获。
  外层用 `except BaseException` 只做收尾然后重新抛出：语义不变
  （REPL 里 Ctrl-C 仍然只取消当前轮），但磁盘上不再留下永远停在 `running`、
  没有 report 的半截 run 目录。
- **one-shot 模式下失败必须非零退出**。CI 依赖这个。

---

## 5. 收尾自检（verify before final）

`--verify-before-final`（默认 on）：模型改了文件却一次验证都没跑过时，
在收尾前拦一次，注入一条"去跑一次验证"的约束。

"算不算跑过验证"由 `verification.py` 统一判定——收尾自检和评测的
`unverified_edit_rate` **必须共用它**，否则两边口径会悄悄分叉。

两个边界条件：

- **只拦一次**。模型如果坚持不验证，硬顶着不让它收尾只会烧完预算。
- 这次运行根本没有能跑验证的工具时（比如 allowlist 里没有 `run_shell`）不拦——
  要求它做一件做不到的事只会白白烧掉一轮。

---

## 6. 停滞检测

`stall.py` 在一个滑动窗口（默认 8 步）里找四种模式：

| 类型 | 判据 |
| --- | --- |
| `repeat_exact` | 同一个动作签名连续出现 3 次 |
| `ab_loop` | A→B→A→B 交替 2 轮 |
| `no_progress` | 连续 4 步没有任何工作区变化或新信息 |
| `error_storm` | 连续 3 步工具报错 |

命中后**注入结构化干预，而不是拒绝执行**：告诉模型检测到了什么模式、
它重复的是哪个动作、建议换一条路。直接拒绝执行只会让模型换个写法再撞一次墙。

另有 `plan_pressure`：计划长期不推进时提醒模型更新 `update_plan`。

---

## 7. 多维预算

`budget.py::RunBudget` 同时盯四个维度：步数、token、时间、金额。
默认全部为 `None`——**不设就完全是老行为，只有 `--max-steps` 生效**。

- 软阈值 **80%**：往 constraints 段注入"该收敛了"。
- 硬阈值 **100%**：不再调模型，直接优雅收尾。

金额有一条硬规矩：**`usd=None` 表示"不知道价格"，绝不能当成 0**。
把未知价格当成免费，等于给了一个永远不会触发的预算上限。

token 用量优先用 provider 返回的真值；后端报了真实 usage 时，
还会记一条 `(我们估了多少 / 后端说是多少)` 进在线校准
（见 [prompt-context](prompt-context.md#7-token-估算与在线校准)）。

---

## 8. 钩子在循环里的位置

| 钩子 | 位置 | 能否改控制流 |
| --- | --- | --- |
| `pre_tool` | 策略检查之后、审批之前 | **能**：退出码 2 = 拒绝，必须记 `hook_denied` |
| `post_tool` | 工具执行之后 | 否，纯观察 |
| `pre_final` | 收尾自检之后、返回之前 | 否，纯观察 |
| `post_run` | run 完全结束后 | 否 |

`pre_tool` 放在审批**之前**：被钩子拒掉的调用不该先去打扰用户按一次 y。
`pre_final` 没有否决权是刻意的——"提交前跑一遍测试"这类用法要的是副作用，
给它否决权等于开了第二条能让 run 永远收不了尾的路径。

---

## 9. 与录制回放的关系

`--record` / `--replay` 是包在 provider client 外面的装饰器，
对主循环**完全透明**。主循环这边只需要守住一件事：
**同样的模型输出必须产生同样的执行顺序**。这就是第 3 节顺序不变量存在的原因。

回放中的 run 在 report 里有 `replay` 字段——一次回放运行必须一眼看得出来，
否则它会被当成真实运行去下结论。

---

## 10. 相关配置

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `--max-steps` | 25 | 单次请求的最大轮数 |
| `--max-input-tokens` / `--max-output-tokens` / `--max-seconds` / `--max-usd` | None | 多维预算 |
| `--parallel-tools` | off | 只读工具批并发 |
| `--verify-before-final` | on | 收尾自检 |
| `--context-mode` | rerender | 每轮重渲染 vs append-only |

完整清单见 [reference/configuration.md](../reference/configuration.md)。
