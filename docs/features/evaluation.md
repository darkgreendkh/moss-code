# 评测框架

> 代码：`moss/evaluation/` · `scripts/eval_*.py` · `benchmarks/`
> 设计稿：[spec-08](../specs/spec-08-evaluation.md)
> 决策：[0003-evidence-layers](../decisions/0003-evidence-layers.md)

评测不在运行时路径上，但它是这个项目里技术含量最高、也最经不起追问的一块。
这一层的全部设计围绕一个反面教材展开：**评测在自证**——
用逐字写死的 `FakeModelClient` 跑出 100% pass rate，
然后拿这个数字去说明"agent 很能干"。那个数字是构造出来的必然结果。

现在的做法是：**把"能证明什么"和"不能据此声称什么"写进每一层的定义里**，
并且用代码强制它们不许混写。

---

## 1. 证据分层 L0–L4

| 层 | 用途 | **不能**据此声称 |
| --- | --- | --- |
| L0 | 单元不变量与安全性质 | 端到端模型能力 |
| L1 | scripted / 回放动作下的 harness 合同 | 模型会自主完成真实任务 |
| L2 | 指定模型、任务集、预算下的能力和成本 | 泛化到其他仓库或模型 |
| L3 | 已知对抗场景下的安全与效用 | 对未知攻击绝对安全 |
| L4 | L2/L3 之后的成本效用与失败集中度 | 无数据支持的产品收益 |

三条硬约束：

- **L0–L4 的证据不得跨层混写。**
- **scripted 入口只属于 L1**，不能声称模型能力。
  `benchmarks/coding_tasks.json` 和旧的 `*-ablation-v2` 是 L1 合同证据
  （它们现在会主动发 `DeprecationWarning` 提醒这一点）。
- **公开 benchmark adapter 不负责下载数据，也不产生或宣称榜单分数**——
  它只转换调用方已合法取得的本地数据。

---

## 2. 快速检查（每次提交）

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
uv run python scripts/eval_lint_tasks.py benchmarks/tasks/mined
uv run python scripts/eval_lint_adversarial.py
```

**全量测试必须零失败。** 环境差异应显式归类或跳过，
不能把固定失败数当成绿色——一旦接受"就那 3 个一直红"，新增的第 4 个红就没人发现了。

---

## 3. L1：从手写脚本到真实轨迹回放

L1 原来靠人手写死每一句模型输出，一改 prompt 就要重写脚本。
现在改由**确定性录制回放**驱动（`moss/providers/recording.py`）：

```bash
moss --record benchmarks/cassettes/p1/<task_id> "任务"
moss --replay benchmarks/cassettes/p1/<task_id> --replay-on-miss fail "任务"
```

`RecordingModelClient` / `ReplayModelClient` 是**包在真实 client 外面的装饰器**，
对主循环透明；身份属性（provider / model / capabilities）必须透传，
否则回放时的能力判定会和录制时不一样。

### 请求指纹

`request_fingerprint` 规范化时剔除：时间戳、run/session id、
workspace 绝对路径前缀、长 hex、耗时。

取舍是**宁可 miss 也不撞车**：
miss 有 `on_miss` 兜底并告警（`replay_miss` 事件），
撞车是**静默回放出一个错误的回答**——后者没有任何信号。

`--replay-on-miss=fail` 是 CI 默认。

### 磁带

`benchmarks/cassettes/<prompt_version>/<task_id>/` **进 git**（小、脱敏过、CI 要用）。
落盘前过 `redact_artifact`。

`manifest.json` 的 `source` 字段区分：

- `scripted-bootstrap` —— 从脚本引导出来的，**不能声称是真实模型轨迹**
- `provider` —— 真实模型录的

`UNCASSETTABLE_TASKS` 登记了两条做不到指纹稳定的任务及原因——
做不到就登记，不糊弄过去。

录制入口：`scripts/record_cassettes.py`。

---

## 4. 任务集：从 git 历史挖掘

```bash
uv run --with pytest python scripts/eval_mine.py --repo . --out benchmarks/tasks/mined --limit 20
uv run python scripts/eval_lint_tasks.py benchmarks/tasks/mined
uv run --with pytest python scripts/eval_audit_tasks.py benchmarks/tasks/mined \
    --repo . --quarantine benchmarks/quarantine.jsonl
```

**所有挖掘和验证使用临时 archive/workspace，不在真实 checkout 上执行候选补丁。**
（历史事故：评测把仓库根的 `README.md` 覆盖成了 `demo`。）

任务先以 `draft` 落盘。只有人工检查过 prompt、许可和 contamination 之后才能改为 `active`。

### 审计

`eval_audit_tasks.py` 做两件事：

- **held-out 检查** —— 任务自带的测试和用来判分的测试必须不同，
  否则 agent 只要让自带测试通过就赢了。
- **mutation 抵抗** —— 往正确答案里注入变异，验证 verifier 抓得住。
  任一 negative patch 通过 → 进入 **append-only quarantine**，
  并**使依赖该题的历史结论失效**。

---

## 5. L2/L3 运行门槛

- 每个 trial 使用**独立进程、独立 workspace、独立 `.moss`**。
- `RunManifest` 固化：commit/diff、模型、解码参数、任务集、fixture、
  预算、workers、sandbox、judge 和**价格日期**。
- artifact 在 manifest 与结果完整写入前保持 `status=incomplete`——
  半截 artifact 不许被当成结果读。
- provider / setup / verifier 的基础设施故障**单列为 `infra_failure`**；
  能力率和端到端可靠率使用**不同分母**。把 provider 超时算成"模型没做出来"
  是最常见的口径错误。
- L2 每行必须记录 token、美元成本（**未知价格为 `null`，不是 0**）、
  wall time、model turns、tool calls。
- L3 同时报**攻击成功率**与 **utility retention**——
  只报前者的话，"把所有工具都禁掉"就是满分方案。
  零事件必须报告 rule-of-three 上界（`3/n`），不许写成"0%"。

统计口径在 `moss/evaluation/stats.py`：pass@1 / pass^k / 置信区间 / 配对检验。

---

## 6. 失败分类

`moss/evaluation/failure_taxonomy.py` 从 **trace 事件**做确定性分类
（TRAIL/MAST 风格），不靠人肉看日志，也不靠模型判断。
分类维度对得上 trace 里已有的事件名——这也是
[`trace_events.py` 禁止写字面量](../architecture.md#5-全局不变量)的直接动机。

---

## 7. Judge

`moss/evaluation/judge.py`：

- judge 只能给 **partial score** 或"建议人工复核"标记，
  **不能覆盖 verifier 的 binary pass**。
- `benchmarks/gold/judge-calibration-v1.json` 是 **50 个待人工盲标槽位**，
  **不是已完成金标**。
- 人工标签齐全后才能计算 κ 与相关系数；**κ < 0.7 时必须标记为未校准**。
- judge 成本超过 L2 总成本 15% 时必须降采样——
  一个比被测系统还贵的评分器不实用。

---

## 8. 对抗套件

`moss/evaluation/adversarial.py` + `benchmarks/adversarial/`：30 个场景，
覆盖 prompt injection、越权路径、危险 shell、记忆投毒、skill 供应链。

`scripts/eval_lint_adversarial.py` 校验场景文件的格式与去重。

---

## 9. 消融

`moss/evaluation/ablations.py` 强制**诚实的消融合同**：
每个消融必须声明基线是什么、关掉的到底是哪一个开关。

项目里几个功能刻意保留了"一键回到从前"的开关，正是为了当消融基线：

| 开关 | 基线含义 |
| --- | --- |
| `--compaction=off` | 纯截断行为 |
| `MOSS_REPO_MAP=off` | 没有 repo map 的 prefix |
| 不配 `--aux-model` | 与加路由前**逐字节一致** |
| `--parallel-tools=off` | 串行执行 |

---

## 10. CI 分级

GitHub 普通 push/PR 运行 **L0/L1**（`.github/workflows/ci.yml`，
Ubuntu × Python 3.10 / 3.12，`ruff check` + `pytest`）。

`.github/workflows/evaluation.yml` 提供每周和手动触发的 **L2/L3 框架门禁**。
真实 provider 运行需要操作者显式提供凭据与预算——
**本仓库不会在普通 CI 中自动消费外部额度**。

---

## 11. 当前证据边界

**已实现并自动验证**：评测卫生、分层、统计、隔离 verifier、价格/成本、
20 个可复现本地任务、held-out/mutation 审计、消融合同、失败分类、
并行 manifest、30 个对抗场景、judge 校准框架、SWE-style adapter。

**尚未产生**：付费真实模型的 L2/L3 结果、ASR/utility 验收数值、
4× wall-clock 实测、50 条人工盲标及 κ≥0.7 的校准结论、公开 benchmark 分数。

这一节必须随实际进展更新。剩余工作见
[plans/active/l2-l3-evidence.md](../plans/active/l2-l3-evidence.md)。
