# 会话、运行工件与恢复

> 代码：`moss/runs/session.py` · `moss/runs/store.py` · `moss/runs/index.py` ·
> `moss/runs/lease.py` · `moss/runs/checkpoint.py` · `moss/runs/ledger.py` ·
> `moss/runs/rewind.py` · `moss/atomic_io.py` · `moss/agent/state.py`
> 设计稿：[spec-07](../specs/spec-07-session-artifacts.md)

两条互相独立的时间线：

- **session** —— 用户视角的对话。跨多次请求，可以 resume、fork、rewind。
- **run** —— 一次 `ask()` 的完整审计记录。一次会话里有很多个 run。

准确的落盘路径见 [reference/storage.md](../reference/storage.md)，
这里讲的是**机制和为什么**。

---

## 1. 原子写：两个 fsync 缺一不可

所有持久化走 `atomic_io.write_atomic`，顺序**不可换**：

```
写临时文件 → fsync(文件) → os.replace → fsync(目录)
```

`os.replace` 只保证"不会出现半截文件"，**不保证落盘**。
两个 fsync 才是"断电不丢"的全部内容——少哪一个都只是把承诺写在注释里。

jsonl 追加的策略是**每条 flush、每 20 条 fsync**：
flush 扛进程被杀，fsync 防断电。20 条的摊派是刻意的取舍——
每条都 fsync 会让 trace 写入变成主要开销。

目录 fsync 在 Windows 上不可用 → **记降级并进 `report.durability_degradations`**。
空列表 = 承诺兑现了。不假装持久，比假装了更有用。

`truncate_partial_tail` 修崩溃留下的半截行。不修的话下一条记录会被粘在它后面，
两条一起读不出来——一次崩溃污染的不是一条记录，而是从此以后的所有记录。

---

## 2. session v2

```
.moss/sessions/<id>/
  meta.json          小对象（memory / runtime_identity / 指针），整份原子写
  history.jsonl      append-only，一条一行
  checkpoints.jsonl  append-only + 定期紧凑化
```

v1 是单文件 `<id>.json`，每轮整份重写 → 写入量 O(n²)。
v2 把大而追加的部分拆成 jsonl，只有小对象整份写。

**自动迁移**：旧的单文件在首次 `load` 时迁移，原件留成 `<id>.json.v1bak`。

`--resume latest` 按 `meta.json` 的 mtime 取最新。
delegate 子 agent 的会话隔离在 `.moss/delegates/`，正是为了不抢这个 latest。

---

## 3. run 目录

```
.moss/runs/<run_id>/
  task_state.json    状态机（status / stop_reason / 计数）
  trace.jsonl        事件时间线，带哈希链
  report.json        结果与关键指标
  lease.json         租约
  artifacts/         卸载的大工具输出
  context/           被 compaction 压掉的原始历史 turns-N.jsonl
  undo/<action_id>/  risky 动作前的旧内容 + manifest.json
```

### task_state

`status`：`running` / `completed` / `stopped` / `failed`。
`stop_reason` 是为什么结束的唯一口径（`final_answer_returned` / `step_limit_reached` /
`model_error` / `budget_exceeded` / `context_overflow` / `interrupted` / …，
完整清单在 `agent/state.py`）。

### trace

事件名**全部来自 `runs/observability/events.py` 的常量**，别处禁止写字面量。
`tests/test_trace_events.py` 用 AST 扫 `moss/evaluation/` 和所有
`emit_trace`/`append_trace` 调用；trace 里出现的名字也必须在 `ALL_EVENTS` 内。

理由：事件名是对外契约，评测脚本按名字匹配。
改一处漏一处的表现不是报错，而是**某个指标悄悄变成 0**，没人会注意到。

每条事件带 `schema_version`（当前 2）与哈希链。
`moss runs verify` 逐条校验，链被改过返回非零。序号与链的计算是 O(1) 的
（早期实现每次全量读 trace，是 O(n²)）。

### report

一次 run 的结果摘要。trace 关注过程，report 关注结果。
关键字段：`stop_reason`、`model_turns` / `tool_calls`、`usage`、
`sandbox`、`durability_degradations`、`truncated_bytes_lost`（应恒为 0）、
`token_calibration`、`compaction_mode`、`replay`、`model_routing`、`hooks`。

---

## 4. run 租约

一台机器上可能同时跑好几个 moss。启动时看到别人的 run 还标着 `running`，
**不能直接判死**——"看到 running 就标 interrupted"在并发下是静默数据损坏。

`lease.json` 记 PID + host + boot_id + 心跳时间，TTL 90 秒。判定链**保守**：

| 情况 | 判定 |
| --- | --- |
| 跨机器（host 不同） | 只看 TTL |
| 机器重启过（boot_id 不同） | 判死 |
| 同机 | 探 `os.kill(pid, 0)` |
| 探到活但心跳超 TTL | 判死 |
| 其余 | **判活** |

最坏结果是"该接管的没接管"（用户手动清理一下），
**绝不误杀活 run**（那会毁掉别人正在进行的工作）。

接管结论分两类，统计里不混算：

- `interrupted` —— 有租约文件且判死。
- `stale` —— 根本没有租约文件（老版本留下的）。

长模型调用或大 pytest 期间，主循环没机会续租 → `LeaseHeartbeat` 独立线程负责。
步边界还有一次同步续租兜底，防止心跳线程在受限环境里起不来。

---

## 5. 动作账本：副作用恰好一次

risky 工具执行**前后各落一条**：

```
action_intent   ← 副作用之前。记 tool / args / 目标路径 / intended_sha
action_receipt  ← 副作用之后。记实际结果 / after_sha
```

`intended_sha`（这次动作做完文件**应该**是什么）是自动对账的前提——
没有它就只能知道"文件变了"，不知道"变成了预期的样子没有"。

崩溃恢复时，"有 intent 无 receipt"的那一段就是**不知道做没做**的窗口：

| 工具类型 | 处理 |
| --- | --- |
| 幂等（`write_file` / `edit_file`） | 比指纹判"已生效"或"可重放" |
| 非幂等（`run_shell`） | **一律 `pending_unknown`，不自动重放** |

宁可多问一次。自动重放一条 `git push` 或 `rm` 的代价，
远高于让用户确认一次的代价。

对账结论记 `action_reconciled`。

---

## 6. undo 与 `/rewind`

同一次执行还会把旧内容存进 `.moss/runs/<id>/undo/<action_id>/`。

`/rewind [n]` 同时回退**文件 + history + memory 快照**——
只回退文件的话，模型的历史里还留着"我已经改好了"，下一轮会基于假前提继续。

一条硬规矩：**回滚前必须比对 `after_sha`**。
用户在 agent 改完之后又动过的文件 → 整个停下等确认，**一个字节都不动**。
`/rewind!` 强制。

用户自己的未提交改动绝不能被悄悄盖掉——这是唯一一类无法从任何工件里恢复的数据。

---

## 7. 恢复、解释与分叉

```bash
moss --resume <id>|latest
moss --resume latest --resume-parts=memory,plan     # 默认全部四项
moss --resume latest --explain                      # 打印会带回什么，然后退出
moss --resume latest --fork <checkpoint_id>         # 从任意 checkpoint 分叉
```

`runs/checkpoint.py` 每步落一个 checkpoint（上限 40，自动裁剪），
恢复部件有四项：`memory` / `plan` / `history` / `checkpoint`。

`evaluate_resume_state` 给出恢复状态：

| 状态 | 含义 |
| --- | --- |
| `full-valid` | 全部可用 |
| `partial-stale` | 工作区指纹变了，部分内容已过期 |
| `workspace-mismatch` | 不是同一个工作区 |
| `schema-mismatch` | checkpoint schema 版本不兼容 |
| `no-checkpoint` | 没有可恢复的 checkpoint |

运行时身份（provider / model / 工具签名 / prompt 版本）不一致时记
`runtime_identity_mismatch`——换了模型再 resume，恢复回来的历史可能已经不适用了。

`--explain` 是这套机制的可解释性出口：**它会明说"会不会重放有副作用的动作"**，
然后退出，不做任何改动。

`--fork` 从任意 checkpoint 分叉出新会话，**原会话不动**。
undo 不跟着走——分叉出来的是一条新时间线，回退旧时间线的文件没有意义。

---

## 8. run 索引与保留

`.moss/runs/index.jsonl`：append-only，读时按 `run_id` 折叠。
**启动只读索引**，不再 glob 全部 `task_state.json`（几百个 run 之后那会明显拖慢启动）。
索引膨胀到 3 倍时紧凑化。索引丢了会自动重建。

保留策略两个维度是**"或"**关系：
`MOSS_RUN_RETENTION_COUNT`（默认 200）/ `MOSS_RUN_RETENTION_DAYS`（默认 30），
设 0 关掉那一维。

过期 run 打包成 `<run_id>.jsonl.gz`（**逐文件 jsonl，解开仍可 grep**），原目录删除。

**永不清理**三类：

- `pinned` 的（`moss runs pin`）
- 持有**有效**租约的（还在跑）
- 被 `artifacts/*.json` 引用的（某份评测结论依赖它做证据）

最后一条是评测可复现性的地基：一份报告引用的 run 被自动清掉，
这份报告就再也无法被复核。

---

## 9. 查看与导出

```bash
moss runs list [--limit 20]
moss runs show <id> [--html]      # --html：单文件、内联 CSS/SVG、零外部请求
moss runs verify [<id>]           # 哈希链被改过返回非零
moss runs prune [--keep-count N] [--keep-days D] [--dry-run]
moss runs pin <id> [--off]
moss runs export <id> [--otel]    # OTLP/JSON，stdlib 生成，只落文件不推 collector
```

`--html` 里**工具输出一律转义**——它是不可信数据，
一份能在浏览器里执行脚本的 trace 查看器是个笑话。

---

## 10. 中断的完整语义

`AgentLoop.run` 外层 `except BaseException`：

```
收尾（stop_reason=interrupted + trace + report + 释放租约）
  → 必然重新抛出
```

收尾函数**自己绝不抛异常**——收尾失败也不能盖掉原始异常，
工件不全总好过丢掉中断原因。

## 11. 相关 trace 事件

`run_started` · `checkpoint_created` · `run_interrupted` · `run_finished` ·
`action_intent` · `action_receipt` · `action_reconciled` · `runtime_identity_mismatch`
