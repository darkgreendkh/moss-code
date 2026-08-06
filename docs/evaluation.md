# Moss 评测操作指南

## 证据分层

| 层 | 用途 | 不能据此声称 |
| --- | --- | --- |
| L0 | 单元不变量与安全性质 | 端到端模型能力 |
| L1 | scripted/回放动作下的 harness 合同 | 模型会自主完成真实任务 |
| L2 | 指定模型、任务集、预算下的能力和成本 | 泛化到其他仓库或模型 |
| L3 | 已知对抗场景下的安全与效用 | 对未知攻击绝对安全 |
| L4 | L2/L3 之后的成本效用与失败集中度 | 无数据支持的产品收益 |

`benchmarks/coding_tasks.json` 和旧 `*-ablation-v2` 是 L1 合同证据，**不代表模型能力**。公开 benchmark adapter 只转换调用方已合法取得的本地数据，不下载数据，也不产生或宣称公开榜单分数。

## 每次提交的快速检查

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
uv run python scripts/eval_lint_tasks.py benchmarks/tasks/mined
uv run python scripts/eval_lint_adversarial.py
```

## 任务挖掘与审计

所有挖掘和验证使用临时 archive/workspace，不在真实 checkout 上执行候选补丁。

```bash
uv run --with pytest python scripts/eval_mine.py --repo . --out benchmarks/tasks/mined --limit 20
uv run python scripts/eval_lint_tasks.py benchmarks/tasks/mined
uv run --with pytest python scripts/eval_audit_tasks.py benchmarks/tasks/mined --repo . --quarantine benchmarks/quarantine.jsonl
```

任务先以 `draft` 落盘。只有人工检查 prompt、许可和 contamination 后才能改为 `active`。任一 negative patch 通过就进入 append-only quarantine，并使依赖该题的历史结论失效。

## L2/L3 运行门槛

- 每个 trial 使用独立进程、workspace 和 `.moss`。
- `RunManifest` 固化 commit/diff、模型、解码、任务集、fixture、预算、workers、sandbox、judge 和价格日期。
- artifact 在 manifest 与结果完整写入前保持 `status=incomplete`。
- provider/setup/verifier 基础设施故障单列为 `infra_failure`；能力率和端到端可靠率使用不同分母。
- L2 每行必须记录 token、美元成本（未知价格为 `null`）、wall time、model turns 和 tool calls。
- L3 同时报攻击成功率与 utility retention；零事件必须报告 rule-of-three 上界。

GitHub 普通 push/PR 运行 L0/L1；`.github/workflows/evaluation.yml` 提供每周和手动 L2/L3 框架门禁。真实 provider 运行需要操作者显式提供凭据与预算，本仓库不会在普通 CI 中自动消费外部额度。

## Judge 与人工金标

judge 只给 partial score 或人工复核建议，不能覆盖 verifier 的 binary pass。`benchmarks/gold/judge-calibration-v1.json` 是 50 个待人工盲标槽位，不是已完成金标。人工标签齐全后才能计算 κ 与相关系数；κ < 0.7 时必须标记为未校准，judge 成本超过 L2 总成本 15% 时必须降采样。

## 当前证据边界

- 已实现并自动验证：评测卫生、分层、统计、隔离 verifier、价格/成本、20 个可复现本地任务、held-out/mutation 审计、消融合同、失败分类、并行 manifest、30 个对抗场景、judge 校准框架、SWE-style adapter。
- 尚未在本次实现中产生：付费真实模型 L2/L3 结果、ASR/utility 验收数值、4× wall-clock 实测、50 条人工盲标及 κ≥0.7 的校准结论、公开 benchmark 分数。
