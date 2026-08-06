# Spec 07 — 会话状态、运行工件与恢复机制

| 项 | 值 |
| --- | --- |
| 状态 | Draft |
| 对应优化章节 | [第 7 章](../plans/archive/2026-agent-upgrade-plan.md)（7.1–7.8） |
| 优先级 | 7.1 / 7.2 / 7.8 是 P0；7.4 / 7.6 / 7.7 是 P1；7.3 / 7.5 是 P2 |
| 依赖 | 无（7.7 的崩溃矩阵需要 [spec-09](spec-09-new-modules.md) 的录制回放做确定性 oracle） |
| 被依赖 | 所有 spec（`moss/trace_events.py` 常量在这里定义） |

## 1. 背景与问题

session 是单个 JSON，**每次 `record()` 整份重写**（100 轮 = 100 次全量写，总写入量 O(n²)）；trace 的序号靠**每次重读整个文件**计算（O(n²)）；启动时把所有 `status=running` 的 run 一律标成 interrupted。

最后这条是并发下的**静默数据损坏**：一个终端开着 REPL 在跑，另一个终端起一次 `moss`，前一个正在进行的 run 就被判死并写入 failed 的 task_state 和 report。

此外：原子写缺 fsync（注释里"断电不丢"的承诺没兑现）；工具**先产生副作用再写记录**，崩溃窗口里无从判断动作是否执行过；checkpoint 只是文本快照，恢复的是"叙事"不是"状态"。

## 2. 目标 / 非目标

**目标**

1. session 拆成目录 + append-only，写入量从 O(n²) 降到 O(n)。
2. trace 序号 O(1)。
3. run 租约/心跳，杜绝并发误标。
4. 动作意图/回执两阶段，恢复时副作用恰好一次。
5. 原子写补 fsync。
6. 恢复可解释、可部分恢复、可分叉。
7. trace 事件常量化 + schema 版本 + 可选 OTel 导出。
8. run 索引 + 保留策略。
9. `/rewind` 回滚。

**非目标**

- 不引入 SQLite 事件溯源 / CAS blob / outbox（见定稿方案 0.4 的拒绝理由）。JSONL 保持可 grep、可 tail、可手改。
- 不做跨主机的分布式租约（只做同机 PID + TTL）。
- 不做"同 run 内确定性续跑模型调用"（恢复仍是开新 run + 继承状态；本 spec 只保证**副作用**不重复）。

## 3. 现状（代码事实）

| 事实 | 位置 |
| --- | --- |
| session 单文件整份重写，无 fsync | [moss/session_store.py:35](moss/session_store.py#L35) |
| `latest()` 按 mtime 取最新 | [moss/session_store.py:41](moss/session_store.py#L41) |
| `_next_trace_sequence` 每次读全量 trace | [moss/run_store.py:151](moss/run_store.py#L151) |
| `mark_interrupted_runs` 把所有 running 标 interrupted，无 PID/租约 | [moss/run_store.py:94](moss/run_store.py#L94) |
| `find_running_runs` glob 全部 `*/task_state.json` | [moss/run_store.py:85](moss/run_store.py#L85) |
| `_write_json_atomic` 用 tmp + `os.replace`，无 fsync | [moss/run_store.py](moss/run_store.py) |
| 工具副作用先于 history/trace/checkpoint 落盘 | [moss/agent_loop.py:153](moss/agent_loop.py#L153)–[:201](moss/agent_loop.py#L201) |
| 每步落 checkpoint，上限 40 | [moss/checkpoint.py](moss/checkpoint.py)、[moss/agent_loop.py:191](moss/agent_loop.py#L191) |
| 5 种 checkpoint 恢复状态 | [moss/checkpoint.py:16](moss/checkpoint.py#L16) |
| `parent_checkpoint_id` 字段存在但未用于组树 | [moss/checkpoint.py](moss/checkpoint.py) |
| `last_prompt_metadata` 只在模型成功返回后更新 | [moss/agent_loop.py:135](moss/agent_loop.py#L135) |
| 评测代码硬编码事件名字面量 | [moss/evaluation/metrics.py](moss/evaluation/metrics.py) |

## 4. 设计

### 4.1 session v2 布局

```
.moss/sessions/<id>/
  meta.json          # id / created_at / memory / runtime_identity / current_checkpoint_id
  history.jsonl      # append-only，一条一行
  checkpoints.jsonl  # append-only + 定期紧凑化
```

```python
class SessionStore:
    def save(self, session): ...                    # 兼容入口：内部转成增量写
    def append_history(self, session_id, entry): ...
    def append_checkpoint(self, session_id, checkpoint): ...
    def save_meta(self, session_id, meta): ...      # 小对象，整份原子写
    def load(self, session_id): ...                 # v1 单文件 / v2 目录都能读
    def latest(self): ...                           # 同时看 *.json 与 */meta.json
```

- `meta.json` 很小（memory + 指针），整份原子写完全可接受。
- `checkpoints.jsonl` 超过 `CHECKPOINT_HISTORY_LIMIT*2` 行时紧凑化（读 → 裁到 40 条 → 原子重写）。
- **v1 → v2 迁移**：`load` 遇到单文件 `<id>.json` 时就地转成目录结构，原文件改名为 `<id>.json.v1bak`。迁移幂等。

### 4.2 原子写补 fsync

```python
def write_atomic(path, data: str, *, encoding="utf-8"):
    """原子且持久地写一个文件。

    为什么存在：os.replace 保证"不出现半截文件"，但不保证数据已经落盘。
    断电场景下，只有 fsync(file) + fsync(dir) 之后才谈得上"不丢"。
    """
    with tempfile.NamedTemporaryFile("w", encoding=encoding, delete=False,
                                     dir=str(path.parent), prefix=path.name + ".",
                                     suffix=".tmp") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)          # Windows 上会抛，捕获后忽略并记一次降级
    finally:
        os.close(dir_fd)
```

放 `moss/atomic_io.py`，`SessionStore.save` / `RunStore._write_json_atomic` / `write_text_atomic`（[moss/tools.py](moss/tools.py)）三处统一走它。Windows 上目录 fsync 不可用 → 捕获并在启动时 stderr 提示一次（**降级要显式**）。

append 类写入（jsonl）：`f.write(line); f.flush(); os.fsync(f.fileno())`，每条都 fsync 会拖慢——策略是**每条 flush，每 N 条或每次 checkpoint 时 fsync**，并在文档里写明这个取舍。

### 4.3 trace 序号 O(1)

```python
class RunStore:
    def __init__(self, root):
        self._sequences: dict[str, int] = {}       # run_id -> last_sequence

    def _next_trace_sequence(self, run_id):
        if run_id not in self._sequences:
            self._sequences[run_id] = self._scan_last_sequence(run_id)   # 只在首次扫一遍
        self._sequences[run_id] += 1
        return self._sequences[run_id]
```

`_scan_last_sequence` 从文件末尾反向读最后一行（`seek` 到末尾往回找 `\n`），不解析全文。

### 4.4 run 租约

```python
# .moss/runs/<run_id>/lease.json
{
  "owner_pid": 12345,
  "host": "mac.local",
  "boot_id": "…",        # Linux: /proc/sys/kernel/random/boot_id；其它平台留空
  "started_at": "...",
  "heartbeat_at": "...",
  "ttl_s": 90
}
```

```python
class RunLease:
    def acquire(self, run_id) -> None: ...
    def heartbeat(self, run_id) -> None: ...    # 主循环每步调用
    def release(self, run_id) -> None: ...      # 收尾（含中断路径）
    def is_alive(self, lease: dict) -> bool: ...
```

`is_alive` 判定：
1. `host` 不同 → 只看 TTL（跨机无法探测进程）；
2. `host` 相同且 `boot_id` 不同（机器重启过）→ 判死；
3. `host`/`boot_id` 相同 → `os.kill(pid, 0)` 探测；`ProcessLookupError` 判死，`PermissionError` 判活（进程存在但不属于当前用户）；
4. 以上判活但 `heartbeat_at` 超过 `ttl_s` → 判死（进程 hang）。

`mark_interrupted_runs` 改为只接管判死的 run，且区分两种结果：`interrupted`（确认中断，有 lease 且判死）与 `stale`（无 lease 文件，来自旧版本或异常）。两者在统计里分开，不混算。

**PID 复用**：`owner_pid` 单独看有复用风险，配合 `started_at` 与 `boot_id` 可以把误判概率压到可忽略。极端情况下最坏结果是"该接管的没接管"（保守方向），不会误杀活 run。

### 4.5 trace 事件常量与 schema

```python
# moss/trace_events.py
TRACE_SCHEMA_VERSION = 2

RUN_STARTED = "run_started"
PROMPT_BUILT = "prompt_built"
MODEL_REQUESTED = "model_requested"
MODEL_PARSED = "model_parsed"
TOOL_EXECUTED = "tool_executed"
CHECKPOINT_CREATED = "checkpoint_created"
RUN_FINISHED = "run_finished"
# spec-02
TOOLS_BATCH_STARTED = "tools_batch_started"
STALL_DETECTED = "stall_detected"
VERIFICATION_REQUESTED = "verification_requested"
BUDGET_EXCEEDED = "budget_exceeded"
RUN_INTERRUPTED = "run_interrupted"
PLAN_UPDATED = "plan_updated"
# spec-03
INJECTION_SUSPECTED = "injection_suspected"
CAPABILITY_DENIED = "capability_denied"
PRECONDITION_FAILED = "precondition_failed"
# spec-04 / 06
CACHE_CAPABILITY_DETECTED = "cache_capability_detected"
TOOL_REGISTRY_DRIFT = "tool_registry_drift"
CONTEXT_COMPACTED = "context_compacted"
# spec-07
ACTION_INTENT = "action_intent"
ACTION_RECEIPT = "action_receipt"

ALL_EVENTS = frozenset({...})     # 用于测试：trace 里出现的事件名必须在此集合内
```

- 每条事件加 `schema_version` 与 `prev_hash`（[spec-03](spec-03-tool-safety.md) §4.7 的审计链）。
- 评测侧一律 `from moss.trace_events import TOOL_EXECUTED`，**禁止字面量**——加一个测试用 AST 扫 `moss/evaluation/` 里的字符串字面量，命中已知事件名就报错。这样改事件名会在导入期炸，而不是指标悄悄变 0。
- 可选 OTel 导出：`moss runs export <id> --otel > spans.json`（纯 stdlib 生成 OTLP/JSON 结构，不引依赖）。

### 4.6 动作意图/回执

执行前后各追加一条 trace 事件：

```python
action_intent = {
  "action_id": "act_<12>",
  "call_id": ...,                    # 原生协议的调用 ID
  "tool": "write_file",
  "args_digest": "sha256:...",
  "idempotency_key": "sha256(prev_receipt_id + args_digest)",
  "capabilities": ["fs_write"],
  "risk": "write",
  "expected_sha": {"src/a.py": "sha256:..."},   # 见 spec-03 的审批回执
  "approval_receipt_id": "...",
}
action_receipt = {
  "action_id": "act_<12>",
  "status": "success | error | partial_success",
  "exit_code": 0,
  "affected_paths": [...],
  "after_sha": {"src/a.py": "sha256:..."},
  "duration_ms": 123,
}
```

**恢复时的 reconcile**（`reconcile_pending_actions(run_id)`）：

| 状态 | 处理 |
| --- | --- |
| 有 intent 有 receipt | 已完成，跳过 |
| 无 intent | 没开始，正常继续 |
| 有 intent 无 receipt，且工具幂等（`write_file`/`edit_file`） | 用 `after_sha` 与当前文件比对：一致 = 已生效（补一条 receipt）；不一致 = 未生效，可安全重放 |
| 有 intent 无 receipt，且工具非幂等（`run_shell`） | **不自动重放**。标 `pending_unknown`，在 `--explain` 与恢复提示里列出，等用户确认 |

`idempotency_key` 保证同一动作重放不产生两条 receipt。

### 4.7 恢复可解释性与分叉

- `moss --resume latest --explain`：打印
  - checkpoint 记录的 freshness vs 当前工作区 diff（哪些文件变了、哪些摘要失效）；
  - runtime identity 哪些字段不匹配；
  - **恢复后会不会重放有副作用的动作**（来自 §4.6 的 `pending_unknown` 列表）。
- `--resume-parts=memory,plan,history`（默认全部）。
- `moss --fork <checkpoint_id>`：用现成的 `parent_checkpoint_id` 组树，从任意 checkpoint 分叉出新 session。
- 未知的高版本 checkpoint → `schema-mismatch` 且**只读、不注入 prompt**。

### 4.8 run 索引与保留

```
.moss/runs/index.jsonl     # {run_id, started_at, status, task_summary, cost_usd, pinned}
```

- 启动只读索引，不再 glob + 解析全部 `task_state.json`。
- 保留策略：最近 200 个 run 或 30 天（`MOSS_RUN_RETENTION_COUNT` / `_DAYS`），过期 run 打包成 `<run_id>.jsonl.gz`（stdlib `gzip`）。
- **永不清理**：`pinned=True`、持有有效租约、被评测工件引用的 run。
- `moss runs list|show|verify|prune|export` 子命令。

### 4.9 `/rewind`

- risky 工具执行前把将被修改文件的旧内容存 `.moss/runs/<id>/undo/<seq>/<path>`（只存 diff 命中的文件，配合 [spec-01](spec-01-repo-context.md) 的增量快照代价可控）；有 git 时优先 `git stash create` 存对象。
- `/rewind [n]`：恢复文件 + 截断 `history.jsonl` 到该 checkpoint + 回滚 memory 快照。
- **用户自己的未提交改动不得被覆盖**：回滚前检测目标文件是否有非 agent 造成的改动（比对 undo 记录的 `after_sha` 与当前 sha），不一致则要求确认。

### 4.10 涉及文件

| 文件 | 改动 |
| --- | --- |
| `moss/atomic_io.py` | 新增 |
| `moss/trace_events.py` | 新增 |
| `moss/lease.py` | 新增 |
| [moss/session_store.py](moss/session_store.py) | v2 目录布局 + 迁移 + append |
| [moss/run_store.py](moss/run_store.py) | 序号 O(1)；租约接管；索引；保留；artifacts/context/undo 路径 |
| [moss/agent_loop.py](moss/agent_loop.py) | intent/receipt；heartbeat；中断释放租约 |
| [moss/checkpoint.py](moss/checkpoint.py) | `--explain` 的 diff 渲染；分叉树 |
| [moss/cli.py](moss/cli.py) | `moss runs ...`、`--explain`、`--resume-parts`、`--fork`、`/rewind` |
| [moss/evaluation/](moss/evaluation/) | 全部改用 `trace_events` 常量 |

## 5. 兼容与迁移

- `SessionStore.save(session)` 签名不变（现有调用点与测试不动），内部改成增量写。
- v1 单文件 session 自动迁移，保留 `.v1bak`；`latest()` 同时识别两种布局。
- 无 `lease.json` 的旧 run 一律按 `stale` 处理（不当作 `interrupted` 计入统计）。
- `TRACE_SCHEMA_VERSION` 从 1 → 2；评测侧读到 v1 事件时按旧字段名兼容解析一个版本周期。
- `.moss/runs/index.jsonl` 不存在时首次启动重建（扫一遍现有 run 目录）。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| `tests/test_session_store_v2.py`（新） | v1→v2 迁移幂等；500 轮会话累计写入量下降 ≥95%（统计 write 调用字节数）；append 后 `load` 结果与旧实现一致 |
| `tests/test_trace_sequence.py`（新） | 写 1000 条事件 <200ms；序号连续无重复；末行反向读正确 |
| `tests/test_lease.py`（新） | 持有有效租约的 run 不被第二个进程标 interrupted（**这是 bug #2 的回归测试**）；PID 不存在 → 可接管；heartbeat 超时 → 可接管；无 lease → `stale` |
| `tests/test_action_receipt.py`（新） | 四个边界（副作用前/后、receipt 前/后）注入 kill，恢复后：文件副作用恰好一次、无重复 shell、`pending_unknown` 被列出 |
| `tests/test_atomic_io.py`（新） | fsync 被调用（mock 计数）；Windows 上目录 fsync 失败被吞且记降级 |
| `tests/test_trace_events.py`（新） | `moss/evaluation/` 下不出现已知事件名的字符串字面量（AST 扫描）；trace 里的事件名都在 `ALL_EVENTS` 内 |
| `tests/test_run_index.py`（新） | 1000 个 run 下启动 <100ms；pinned/被引用的 run 不被 prune |
| `tests/test_rewind.py`（新） | rewind 后工作区与 session 和第 n 步结束时逐字节一致；用户 dirty 改动被保留并触发确认 |
| `tests/test_resume_explain.py`（新） | `--explain` 输出包含 freshness diff、identity 不匹配项、待重放动作 |

## 7. 验收标准

| 指标 | 门槛 |
| --- | --- |
| 500 轮会话累计写入量 | 下降 ≥95% |
| 1000 条 trace 事件写入耗时 | <200ms（线性） |
| 并发 run 误标率 | 0 |
| 崩溃后副作用重复次数 | 0 |
| 中断工件完整率 | 100%（含 `stop_reason` 与租约释放） |
| 1000 个 run 下启动耗时 | <100ms |
| rewind 一致性 | 逐字节相同 |
| 事件名改动 | 导入期报错（不再静默失真） |

## 8. 实施顺序（PR 拆分）

1. **PR-1（P0，S）**：`moss/lease.py` + `mark_interrupted_runs` 改造 + 回归测试。**并发损坏优先修**。
2. **PR-2（P0，S）**：trace 序号 O(1)。
3. **PR-3（P0，S）**：`moss/atomic_io.py` + 三处统一 + fsync 测试。
4. **PR-4（P0，M）**：session v2 目录 + 迁移。
5. **PR-5（P1，S）**：`moss/trace_events.py` + 评测侧全量替换 + AST 扫描测试。
6. **PR-6（P1，S）**：run 索引 + 保留策略 + `moss runs list/show/prune`。
7. **PR-7（P1，M）**：action intent/receipt + reconcile。
8. **PR-8（P1，M）**：`--explain` / `--resume-parts` / `--fork`。
9. **PR-9（P2，S）**：trace hash 链 + `moss runs verify`（与 [spec-03](spec-03-tool-safety.md) PR-7 合并）。
10. **PR-10（P2，L）**：`/rewind` + undo 存储。
11. **PR-11（P2，M）**：OTel 导出。

## 9. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| session 迁移丢数据 | `.v1bak` 备份 + 幂等 + 迁移前后 history 条数断言 |
| 租约误判活进程为死 | 判定链保守（探测不到就判活）；最坏是"该接管没接管"，不误杀 |
| PID 复用导致误判 | `started_at` + `boot_id` 联合判定；概率可忽略且方向保守 |
| 每条 jsonl 都 fsync 拖慢 | 每条 flush、每 N 条 fsync；阈值可配；文档写明取舍 |
| intent/receipt 让 trace 体积翻倍 | 两条事件都很小（<300 字节）；配合 run 保留策略 |
| `reconcile` 误判 shell 已执行 | 非幂等工具一律不自动重放，转人工——宁可多问一次 |
| Windows 上 fsync/dir_fd 不可用 | 显式降级 + 启动提示；不假装持久 |

## 10. 开放问题

1. `history.jsonl` 是否要按大小分片？倾向：暂不，先看 500 轮会话的实际文件大小。
2. 租约 TTL 90s 是否够？长工具（大 pytest）会不会超时？倾向：heartbeat 在工具执行期间由独立定时线程刷新，避免长工具期间租约过期。
3. `--fork` 出来的 session 是否要复制 undo 目录？倾向：不复制，fork 只继承状态不继承回滚能力，并在提示里说明。
4. OTel 导出要不要支持直接推送到 collector？倾向：只落文件，推送交给用户的工具链（避免网络能力进核心）。
