# 待完成：L2/L3 真实证据

- **状态**：进行中
- **开始**：2026-08-06
- **前置**：全部已就位（框架、任务集、verifier、统计、manifest、judge 框架、对抗场景）
- **相关**：[features/evaluation.md](../../features/evaluation.md) · [decisions/0003](../../decisions/0003-evidence-layers.md)

## 为什么这份还开着

[2026 大改方案](../archive/2026-agent-upgrade-plan.md) 的九个章节都已落地
（spec-01 到 spec-09 全部实现并有测试）。**但评测这条线只完成了一半**：
框架、隔离、统计口径、成本记账、对抗场景、judge 校准框架都在了，
**真实模型跑出来的数字一个都没有**。

这不是遗漏，是刻意停在这里的：L2/L3 需要付费 provider 额度和人工标注时间，
两者都不能靠写代码解决。把框架当成结论用，正是
[decisions/0003](../../decisions/0003-evidence-layers.md) 要防的事。

## 缺口清单

| # | 缺什么 | 阻塞在 | 完成判据 |
| --- | --- | --- | --- |
| 1 | 付费真实模型的 L2 结果 | provider 额度与预算决定 | 至少一个模型 × 20 个 mined 任务，带 manifest、pass@1 与置信区间、每行成本 |
| 2 | L3 的 ASR / utility retention 验收数值 | 同上 | 30 个对抗场景跑完，同时报攻击成功率与效用保持率，零事件报 rule-of-three 上界 |
| 3 | 并行加速的实测 | 需要 1 的运行数据 | `--parallel-tools` 的 wall-clock 实测（此前只有"4×"的设计预期，未验证） |
| 4 | judge 校准 | 需要人工盲标 | `benchmarks/gold/judge-calibration-v1.json` 的 50 个槽位标完，算出 κ 与相关系数；κ ≥ 0.7 才能声称已校准 |
| 5 | 公开 benchmark 分数 | 数据许可 + 复现条件 | 本仓库**不打算**产出这一项，见 [decisions/0003](../../decisions/0003-evidence-layers.md#公开-benchmark) |

第 5 项列在这里是为了防止它被反复提出——它是**明确的非目标**，不是待办。

## 做的时候必须守住的

这些在框架里已经是代码级约束，跑之前再确认一遍：

- 每个 trial 独立进程 / 独立 workspace / 独立 `.moss`。
- `RunManifest` 固化 commit/diff、模型、解码参数、任务集、fixture、
  预算、workers、sandbox、judge 和**价格日期**。
- artifact 在 manifest 与结果完整写入前保持 `status=incomplete`。
- infra failure 单列，能力率与端到端可靠率**不同分母**。
- 未知价格写 `null`，**不写 0**。
- 结果落盘的 run 记得 `moss runs pin`——
  保留策略永不清理被 `artifacts/*.json` 引用的 run，但 pin 一下更稳。

## 先做哪个

1 和 2 共用一次运行环境准备，应该一起做。
3 是 1 的副产品。4 独立，可以随时开始（它需要的是人的时间，不是额度）。

## 下一步

跑 L2 之前先用磁带回放做一次全链路演练：
`--replay ... --replay-on-miss fail` 能验证 runner、manifest、
统计和报告生成这一整条路是通的，且**零成本**。
演练不通就不该去烧真钱。
