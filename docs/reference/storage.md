# 落盘结构参考

`.moss/` 在工作区根目录，**整个目录被 gitignore**。
全局记忆在 `~/.moss/memory/`。

一条总规矩：**所有持久化走 `atomic_io.write_atomic`**
（临时文件 → fsync 文件 → `os.replace` → fsync 目录），不要在别处再手写一份 tmp+replace。

---

## 1. 全景

```
<workspace>/.moss/
├── sessions/<session_id>/           用户会话 v2
│   ├── meta.json                    小对象（memory / runtime_identity / 指针）
│   ├── history.jsonl                append-only
│   └── checkpoints.jsonl            append-only + 定期紧凑化
├── sessions/<session_id>.json.v1bak 迁移后留下的 v1 原件
├── delegates/<session_id>/          delegate 子 agent 的一次性会话（隔离）
├── runs/
│   ├── index.jsonl                  run 索引（append-only，读时按 run_id 折叠）
│   ├── <run_id>.jsonl.gz            过期 run 的归档
│   └── <run_id>/
│       ├── task_state.json
│       ├── trace.jsonl
│       ├── report.json
│       ├── lease.json
│       ├── artifacts/               卸载的大工具输出
│       ├── context/turns-N.jsonl    被 compaction 压掉的原始历史
│       └── undo/<action_id>/        risky 动作前的旧内容 + manifest.json
├── memory/                          项目级 durable 记忆
│   ├── records.jsonl                事实源（append-only）
│   ├── MEMORY.md                    派生索引
│   ├── topics/  procedural/  episodic/
│   └── aliases.md
├── cache/
│   ├── repo_map.json
│   ├── token_calibration.json
│   └── skill_trust.json
├── skills/*.md                      技能
├── hooks/<point>                    用户钩子（需可执行位）
├── prompts/system.md                覆盖内置 p1 system head
└── config.json                      结构化配置

~/.moss/memory/                      全局 durable 记忆（同上结构）
<workspace>/.env                     本地密钥（仓库只保留 .env.example）
```

---

## 2. sessions/

| 文件 | 写法 | 内容 |
| --- | --- | --- |
| `meta.json` | 整份原子写 | 会话元信息、working/episodic memory、runtime identity、指向 jsonl 的游标 |
| `history.jsonl` | append，每条 flush / 每 20 条 fsync | 一条对话或工具结果一行 |
| `checkpoints.jsonl` | append + 紧凑化 | 每步一个 checkpoint |

`meta.json` 里**不放** history / checkpoints——它们各自有 append-only 文件。
v1 是单文件 `<id>.json` 每轮整份重写（写入量 O(n²)），
首次 `load` 时自动迁移，原件留成 `<id>.json.v1bak`。

`checkpoints.jsonl` 超过 `CHECKPOINT_HISTORY_LIMIT × 2 = 80` 行时紧凑化。

`--resume latest` 按 `meta.json` 的 **mtime** 取最新。

### delegates/

结构与 `sessions/` 相同，但**隔离**：
delegate 子 agent 的一次性会话如果写进 `sessions/`，
刚写完就会抢在用户会话前面被 `--resume latest` 选中。

---

## 3. runs/

### index.jsonl

append-only，读时按 `run_id` 折叠。每条的字段：

```
run_id  started_at  status  stop_reason  task_summary  cost_usd  pinned
```

启动只读它，不 glob 全部 `task_state.json`。
膨胀到有效条数 3 倍时紧凑化。删了会自动重建（`ensure_index`）。

### `<run_id>/task_state.json`

| 字段 | 取值 |
| --- | --- |
| `status` | `running` / `completed` / `stopped` / `failed` |
| `stop_reason` | `final_answer_returned` / `step_limit_reached` / `retry_limit_reached` / `model_error` / `tool_timeout` / `approval_denied` / `delegate_failed` / `persistence_error` / `resume_load_error` / `interrupted` / `budget_exceeded` / `context_overflow` |
| 计数 | `tool_steps` / `attempts` / `model_turns` / `tool_calls` |
| 指针 | `checkpoint_id` / `resume_status` |

### `<run_id>/trace.jsonl`

一行一个事件，带 `schema_version`（当前 **2**）、序号和哈希链。
事件名必须来自 `trace_events.py` 的 `ALL_EVENTS`。
`moss runs verify` 逐条校验链，被改过返回非零。

### `<run_id>/report.json`

一次 run 的结果摘要。值得记住的字段：

| 字段 | 含义 |
| --- | --- |
| `stop_reason` / `status` | 怎么结束的 |
| `model_turns` / `tool_calls` | 成本与失败率口径（`tool_steps` 语义不变，这两个只加不改） |
| `usage` | 预算快照 |
| `run_manifest` | prompt_version / provider / model / tool_protocol / context_mode |
| `sandbox` | 实际生效的沙箱层。**降级必须看得见** |
| `durability_degradations` | 持久化降级（如 Windows 上目录 fsync 不可用）。空列表 = 承诺兑现 |
| `truncated_bytes_lost` | **应恒为 0**（卸载生效的判据） |
| `error_signal_lost_count` | 压缩把失败信号压没了的次数 |
| `token_calibration` | 在线校准状态 |
| `compaction_mode` / `compactions` | 压缩了几次、用的哪种 method |
| `replay` | 非空 = 这是一次回放运行，**不能当真实运行下结论** |
| `model_routing` | aux 路由用了几次、降级过没有 |
| `hooks` | 钩子跑过没有、拒过没有 |
| `durable_promotions` / `_rejections` / `_superseded` / `procedural_distilled` | 记忆系统这次做了什么 |
| `redacted_env` | 检测到并脱敏了哪些 secret 名 |

### `<run_id>/lease.json`

`{pid, host, boot_id, started_at, heartbeat_at}`，TTL 90 秒。
判活链见 [features/sessions-and-runs.md](../features/sessions-and-runs.md#4-run-租约)。

### `<run_id>/artifacts/`

超过 `ARTIFACT_THRESHOLD`（= `MAX_TOOL_OUTPUT` = 16000 字符）的工具输出，脱敏后按内容 **sha12 去重**落盘。
prompt 里只放压缩摘要 + `read_artifact` 指针。
`read_artifact` 的 `path_scope="run_dir"`——它只能读这里，不能读仓库。

### `<run_id>/context/turns-N.jsonl`

被 compaction 压掉的**原始**历史。这是"压缩可逆"的物理保证，
`read_artifact` 可以逐字取回。

### `<run_id>/undo/<action_id>/`

risky 动作执行前的旧内容 + `manifest.json`（含 `after_sha`）。
`/rewind` 回滚前必须比对 `after_sha`——
用户自己的未提交改动绝不能被悄悄盖掉。

### `<run_id>.jsonl.gz`

过期 run 的归档：**逐文件 jsonl**，解开之后仍然可以 grep。
永不归档：pinned / 持有有效租约 / 被 `artifacts/*.json` 引用的 run。

---

## 4. memory/

| 文件 | 角色 |
| --- | --- |
| `records.jsonl` | **事实源**。append-only，读时折叠；超过 2000 条紧凑化 |
| `MEMORY.md` | 派生索引 |
| `topics/` `procedural/` `episodic/` | 派生视图 |
| `aliases.md` | 别名表 |

派生文件都可以从 `records.jsonl` 重建。

一条记录的字段：`id` / `scope`（`global`\|`project`\|`path`\|`session`）/ `scope_key` /
`topic` / `subject` / `text` / `tags` / `trust`（`user`\|`model`\|`tool`）/
`confidence` `[0,1]` / `status`（`active`\|`superseded`\|`needs_review`\|`tombstone`）/
`supersedes` / `source_refs` / 时间戳 / 命中计数。

读 jsonl 时只忽略**最后半行**（进程在 append 中途被杀）；
中间的坏行直接抛——那说明文件被别的东西破坏了。

---

## 5. cache/

**可以随时整个删掉，会自动重建。**

| 文件 | 内容 |
| --- | --- |
| `repo_map.json` | 目录骨架 + 符号索引（schema `repo-map-v1`） |
| `token_calibration.json` | 按 `(provider, model)` 分桶的最近 50 条 `(估算, 真值)` |
| `skill_trust.json` | 第三方 skill 的内容指纹台账（指纹覆盖 frontmatter + 正文） |

---

## 6. skills / hooks / prompts / config

| 路径 | 说明 |
| --- | --- |
| `skills/*.md` | frontmatter：`name` / `description` / `allowed-tools` / `scope` / `resources` / `source`；正文按 `use_skill` 懒加载 |
| `hooks/<point>` | `pre_tool` / `post_tool` / `pre_final` / `post_run`，**只认可执行位**，超时 3s |
| `prompts/system.md` | 覆盖内置 `p1`；版本记为 `file:<sha256前12位>` |
| `config.json` | 见 [configuration.md](configuration.md#4-mossconfigjson) |

**agent 写不进 `.moss/`**（`policy.DEFAULT_DENY` 覆盖了 `fs_write`），
否则它能给自己装钩子。

---

## 7. 仓库里的（进 git）

| 路径 | 说明 |
| --- | --- |
| `benchmarks/cassettes/<prompt_version>/<task_id>/` | 录制磁带。小、脱敏过、CI 要用，所以进 git |
| `benchmarks/tasks/` `benchmarks/adversarial/` `benchmarks/gold/` | 任务集、对抗场景、待标注金标 |
| `benchmarks/quarantine.jsonl` | append-only 隔离区 |
| `.env.example` | 模板；真的 `.env` 被忽略 |
