# 0003 · 评测证据分 L0–L4，不许跨层混写

- **状态**：已采纳
- **影响范围**：`moss/evaluation/`、`benchmarks/`、README 与所有对外结论
- **相关**：[features/evaluation.md](../features/evaluation.md) · [spec-08](../specs/spec-08-evaluation.md)

## 背景：评测在自证

改造之前，项目的核心 benchmark 用一个逐字写死的 `FakeModelClient`：
每一步模型"说"什么都是脚本里写好的。跑出来的 `pass_rate = 100%`
是构造出来的必然结果，它证明的是"harness 能按给定动作执行"，
但它被当作"agent 能完成任务"在用。

memory / recovery 消融实验也有同样的问题：
所谓"正确性"判据本质是"prompt 里有没有出现这个字符串"。

这是整个项目里技术含量最高、也最经不起追问的一块。

## 决定

给每一层证据写死"能证明什么"和**"不能据此声称什么"**，并用代码强制：

| 层 | 用途 | **不能**据此声称 |
| --- | --- | --- |
| L0 | 单元不变量与安全性质 | 端到端模型能力 |
| L1 | scripted / 回放动作下的 harness 合同 | 模型会自主完成真实任务 |
| L2 | 指定模型、任务集、预算下的能力和成本 | 泛化到其他仓库或模型 |
| L3 | 已知对抗场景下的安全与效用 | 对未知攻击绝对安全 |
| L4 | L2/L3 之后的成本效用与失败集中度 | 无数据支持的产品收益 |

强制手段不是"写在文档里提醒大家注意"，而是：

- scripted 入口（`run_*_ablation_v2` 等）主动发 `DeprecationWarning`，
  明说"这是 L1 兼容实验，能力结论请用 L2"。
- 磁带 `manifest.json` 的 `source` 区分 `scripted-bootstrap` 与 `provider`——
  从脚本引导出来的磁带**不能声称是真实模型轨迹**。
- L2/L3 的 artifact 在 manifest 与结果完整写入前保持 `status=incomplete`。
- `moss/evaluation/` 里的报告生成器会拒绝没有统计区间、没有成本字段的声明。

## 三条最容易犯的口径错误

这三条单独列出来，因为它们看起来都很合理：

### 1. 把 infra failure 算进能力分母

provider 超时、setup 失败、verifier 自己崩了——这些不是"模型没做出来"。
它们单列为 `infra_failure`，**能力率和端到端可靠率用不同分母**。
混算的结果是模型换个网络环境分数就变了。

### 2. 未知价格当成 0

`usd=None` 表示"不知道这个模型多少钱"。当成 0 的话，
成本图上它就是免费的，`--max-usd` 预算永远不会触发。
未知就写 `null`，宁可少一行数据。

### 3. 零事件写成 0%

对抗评测里 30 个场景一个都没攻破，不等于"攻击成功率 0%"。
必须报 rule-of-three 上界（`3/n`）。同理，L3 必须**同时**报
攻击成功率和 utility retention——只报前者的话，
"把所有工具都禁掉"是满分方案。

## 判分权

**judge 不能决定 binary pass。** 它只能给 partial score 或"建议人工复核"标记。
binary pass 由隔离的 verifier 给出，跑在独立进程和独立 workspace 里。

judge 自己也要被校准：κ < 0.7 时必须标记为"未校准"，
`benchmarks/gold/judge-calibration-v1.json` 现在是 **50 个待人工盲标槽位**，
**不是已完成金标**——这一点必须在任何引用它的地方说清楚。

## 公开 benchmark

公开 adapter **不负责下载数据**，也**不产生或宣称榜单分数**。
它只转换调用方已合法取得的本地数据。

原因有两条：数据许可不由本仓库承担；
以及一个没有严格复现条件的"SWE-bench 得分"是纯粹的噪声。

## 代价

这套规矩让"能拿出来说的数字"变少了很多。
[features/evaluation.md](../features/evaluation.md#11-当前证据边界)
里有一节专门写"尚未产生"的证据——
一份诚实的空白，比一个自证的 100% 有用。
