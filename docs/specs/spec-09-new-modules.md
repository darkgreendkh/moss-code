# Spec 09 — 新增功能模块

| 项 | 值 |
| --- | --- |
| 状态 | Implemented（8 个模块全部落地；9.3 code mode 默认关闭且以沙箱为硬前置） |
| 对应优化章节 | [第 9 章](docs/optimize/2026-agent-upgrade-plan.md)（9.1–9.8） |
| 优先级 | 9.8 是 P0；9.1 / 9.4 / 9.7 是 P1；9.2 / 9.3 / 9.5 / 9.6 是 P2 |
| 依赖 | 各子模块不同，见每节 |
| 被依赖 | [spec-08](spec-08-evaluation.md)（录制回放是 L1 的基础设施）、[spec-06](spec-06-context.md)（aux model 用于 compaction） |

这份 spec 覆盖 8 个相对独立的新模块。每个模块自带目标 / 接口 / 验收 / 风险，可独立立项、独立回滚。**共同约束**：全部零第三方依赖（外部能力一律探测 + 降级）、全部进同一套护栏（能力标签、审批、脱敏、trace）、全部有 trace 事件。

---

## 9.1 sub-agent 上下文隔离的正规化 · `[P1][M]`

**依赖**：[spec-02](spec-02-agent-loop.md)（并发设施）、[spec-03](spec-03-tool-safety.md)（能力继承）

**现状**：`delegate` 已有雏形——只读、`max_steps=3`、会话隔离到 `.moss/delegates/`（[moss/runtime.py:506](moss/runtime.py#L506)），但把父 history 硬 clip 到 300 字符塞进 notes（[moss/runtime.py:525](moss/runtime.py#L525)），返回也只是裸文本拼接 `"delegate_result:\n" + child.ask(task)`。

**设计**

```python
@dataclass(frozen=True)
class DelegateContract:
    goal: str
    allowed_tools: tuple[str, ...]
    capabilities: frozenset[str]      # 必须是父能力的严格子集
    max_steps: int
    max_usd: float | None
    context_seed: tuple[str, ...]     # 结构化的必要背景，不是截断的 history

@dataclass(frozen=True)
class DelegateResult:
    findings: tuple[Finding, ...]     # {claim, evidence_path, line_range}
    unresolved: tuple[str, ...]
    confidence: float
    cost: dict                        # tokens / usd / wall_s / steps
```

- `context_seed` 由父 agent 显式构造（当前任务目标 + 相关文件路径列表），**不再截断父 history**——这正是 sub-agent 作为上下文治理手段的意义。
- 返回结构化 `findings`，每条带证据锚点，父 agent 可直接 `read_file(path, start, end)` 核验。
- 独立预算（步数 + token + $），计入父 run 的总账。
- 可并行 fan-out：`ThreadPoolExecutor` 复用 [spec-02](spec-02-agent-loop.md) §4.1 的设施，结果按提交顺序聚合。
- **保持只读**。可写子 agent 要等 [spec-03](spec-03-tool-safety.md) 的沙箱 + [spec-07](spec-07-session-artifacts.md) 的动作幂等都到位，否则只是把一个不可恢复的循环复制多份。

**验收**：同一"搜索 + 汇总"任务，delegate 路径相对主 agent 直接做，父 context token 下降 ≥40% 且结论质量不降（judge 判定）；子 agent 能力集是父集的严格子集（断言测试）。

---

## 9.2 MCP 客户端与服务端 · `[P2][L]`

**依赖**：[spec-03](spec-03-tool-safety.md)（能力标签、单一入口）

**设计**

- **客户端**：stdio + JSON-RPC 2.0，用 stdlib `subprocess` + `json` 实现（约 300 行）。`.moss/config.json` 的 `mcp.servers` 声明要启动的 server。
- **注册期显式落白名单**：MCP 暴露的工具在启动时被转成 `ToolSpec` 并写入注册表，进入 `tool_signature`。**不做运行期动态发现**（CLAUDE.md 的既定约定）。
- 外部工具**必须声明 capabilities**，否则 fail-closed 拒绝（[spec-03](spec-03-tool-safety.md) §4.2）。默认 `{network}` + 审批。
- 工具数膨胀会撑爆 prefix → 超过 N 个（默认 12）时切换为"工具名 + 一句话"目录 + `describe_tool(name)` 按需取完整 schema。
- **服务端**：把 moss 自己暴露成 MCP server（`list_files` / `read_file` / `search_text`），走 `Moss.execute(ActionRequest)` 这个唯一入口（[spec-03](spec-03-tool-safety.md) §4.3），护栏一视同仁。

**验收**：接一个真实 MCP server（如 filesystem）跑通；外部工具的越权调用被 policy 拒绝；工具数 30 时 prefix token 增长 <20%。

**风险**：MCP server 是外部进程，输出属于**不可信数据**——必须走 [spec-03](spec-03-tool-safety.md) §4.4 的 `<tool_result untrusted>` 包裹与注入扫描。

---

## 9.3 代码执行式工具编排（code mode） · `[P2][L]`

**依赖**：[spec-03](spec-03-tool-safety.md) §4.6 的沙箱（**硬前置**，没有沙箱不做）

**设计**

- 新工具 `run_orchestration(script)`：模型写一段受限 Python，一次性编排多个工具调用（"读这 5 个文件里所有含 X 的函数并汇总"），替代 5 轮往返。
- 脚本运行在受限命名空间里，**只能访问注入的工具 API**（`fs.read(path)`、`search(pattern)`、`emit(result)`），拿不到 `import`、`open`、`__builtins__`。
- 实现方式：`compile()` + 白名单 AST 校验（禁 `Import`、`Attribute` 访问 dunder、`exec`/`eval`），再在沙箱进程里跑，硬超时。
- 每次工具 API 调用仍然逐条走 `ToolExecutor`（审批、脱敏、trace 一个不少），脚本只是把多次调用打包。

**验收**：一个"跨 5 文件汇总"任务，model_turns 从 6 降到 2；AST 白名单拒绝所有逃逸样例（≥20 条）。

**风险**：这是本 spec 里风险最高的一项。**没有 [spec-03](spec-03-tool-safety.md) 的 L2/L3 沙箱就不要上**；默认关闭，`--enable-code-mode` 显式开启。

---

## 9.4 Skills 体系强化 · `[P1][S]`

**依赖**：[spec-03](spec-03-tool-safety.md)（能力标签）

**现状**：skill 只是 `.moss/skills/*.md` 的 frontmatter（name/description）+ 全文注入，body 硬 clip 4000（[moss/skills.py](moss/skills.py)）。

**设计**

```yaml
---
name: run-benchmarks
description: 跑基准并生成报告
allowed-tools: [run_shell, read_file]     # 用这个 skill 时的能力收紧/放开
scope: ["benchmarks/**"]                  # 在这些路径下自动提示
resources: ["scripts/bench.sh"]           # 附件，按需 read_file
---
```

- **渐进披露三级**：description（常驻 prefix）→ body（`use_skill` 时注入）→ resources（模型显式 `read_file` 取）。
- `allowed-tools` 生效期间是**能力的临时覆盖**，退出 skill 恢复；只能收紧或在父能力集内放开，不能越权。
- `scope` 命中时在 prompt 里加一行提示（"当前路径下有可用 skill: X"），而不是自动注入全文。
- **供应链**：第三方 skill 记 `source` 与内容 sha256；内容变化后首次使用需要用户确认；能力声明进审批摘要。

**验收**：skill 数量 20 时 prefix 增量 <400 token；`allowed-tools` 越权声明被拒；skill 内容篡改触发确认。

---

## 9.5 Hooks / 事件扩展点 · `[P2][S]`

**依赖**：[spec-07](spec-07-session-artifacts.md)（trace 事件常量）

**现状**：`progress_observer` 只用于展示（[moss/runtime.py:364](moss/runtime.py#L364)），且有"异常必须吞掉"的既有纪律。

**设计**

- 钩子点：`pre_tool` / `post_tool` / `pre_final` / `post_run`，执行 `.moss/hooks/<point>.sh`（或 `.py`）。
- **超时 3s**，失败不阻断控制流（沿用 observer 纪律）；但 `pre_tool` 允许通过退出码 `2` 表达"拒绝这次调用"——这是唯一能影响控制流的钩子，且必须在 trace 里记 `hook_denied`。
- 钩子拿到的是**脱敏后的 JSON**（stdin），不给原始 secret。
- 钩子本身是用户放进 `.moss/hooks/` 的可执行文件——**agent 不能写 `.moss/`**（[spec-03](spec-03-tool-safety.md) §4.2 的默认 deny 覆盖了这条），否则 agent 能给自己装后门。

**典型用法**：写完必跑 `ruff`、提交前必跑测试、敏感路径二次确认。

**验收**：钩子超时不阻断主流程；`pre_tool` 退出码 2 拒绝调用并记事件；agent 无法写入 `.moss/hooks/`。

---

## 9.6 本地 trace 可视化 · `[P2][S]`

**依赖**：[spec-07](spec-07-session-artifacts.md)（trace schema）

**设计**：`moss runs show <id> --html > run.html`，纯 stdlib 字符串拼接生成**单文件** HTML（内联 CSS/JS，无外部资源）：

- 时间线：每步的 prompt 段落构成（各段 token 占比条）、工具调用与结果摘要、耗时、token/成本；
- 失败标签（[spec-08](spec-08-evaluation.md) §4.7）高亮；
- 上下文健康度曲线（[spec-06](spec-06-context.md) §4.5）用内联 SVG 画；
- 敏感内容已在落盘时脱敏，HTML 不做二次处理但要在页眉标注"含脱敏后的工具输出，勿外传"。

**验收**：一个 25 步 run 生成的 HTML <500KB；离线打开正常；不含任何外部请求。

---

## 9.7 多模型路由：便宜模型干脏活 · `[P1][M]`

**依赖**：[spec-04](spec-04-prompt-cache.md)（能力表）、[spec-08](spec-08-evaluation.md)（成本记账）

**设计**

```python
AUX_TASKS = {"compaction", "reflection", "failure_labeling", "judge", "budget_summary"}

def route(task_kind: str, agent) -> ModelClient:
    """脏活走 aux model，主线走主模型。

    为什么存在：compaction 摘要、失败分类、记忆提炼、judge 都不需要主力模型，
    但它们的调用次数可能比主线还多。
    """
```

- `--aux-model` / `--aux-provider`（可以是本地 Ollama 小模型）；未配置时全部回落主模型（行为不变）。
- 每次路由在 trace 记 `model_routed{task_kind, model, reason}`。
- **路由策略的任何改动都要走配对评测**（[spec-08](spec-08-evaluation.md) §4.5），不能凭感觉说"省钱了"。
- aux model 的输出**不进主线 history**，只进它服务的那个子系统（compaction artifact / 记忆记录 / 失败标签）。

**验收**：开启 aux model 后，同任务集总成本下降 ≥25% 且 pass_rate 不下降（配对检验）；aux 失败时自动回落主模型且记事件。

---

## 9.8 确定性录制回放（record & replay） · `[P0][M]`

**依赖**：[spec-02](spec-02-agent-loop.md)（批执行的顺序不变量）、[spec-04](spec-04-prompt-cache.md)（`ModelRequest`）

**这是把 L1 从"人工编脚本"升级成"真实轨迹回放"的桥梁，也是整份方案里性价比最高的单点改动之一。**

**现状**：`FakeModelClient` 需要人手写死每一句模型输出（[moss/evaluation/evaluator.py:47](moss/evaluation/evaluator.py#L47)），改一次 prompt 就要重写全部脚本。

**设计**

```python
# moss/providers/recording.py
class RecordingModelClient:
    """包在真实 client 外面，把每次调用落盘。"""
    def __init__(self, inner, cassette_dir): ...

class ReplayModelClient:
    """按请求指纹回放，未命中按策略处理。"""
    def __init__(self, cassette_dir, *, on_miss="fail"):  # fail | nearest | passthrough
        ...

def request_fingerprint(request: ModelRequest) -> str:
    """规范化后的请求指纹。

    规范化：剔除时间戳、run_id、绝对路径前缀、cwd 这些每次都变但不影响语义的字段，
    再对 (system blocks, messages, tools, max_new_tokens) 做 sha256。
    """
```

**磁带格式**（`benchmarks/cassettes/<name>/`）：

```
manifest.json     # 录制时间、provider、model、agent_commit、prompt_version、脱敏说明
000-<fp12>.json   # {"fingerprint": ..., "request_digest": ..., "response": ..., "usage": {...}}
001-<fp12>.json
```

- 落盘前过 `redact_artifact`（磁带会进仓库，绝不能带 secret）。
- `on_miss="nearest"` 时按指纹的最长公共前缀找最近邻并**在 stderr 告警 + trace 记 `replay_miss`**；`"fail"` 是 CI 的默认值。
- CLI：`moss --record <dir>` / `moss --replay <dir>`；评测侧 `l1_contract.py` 直接消费磁带。

**收益**

1. L1 回归用**真实模型轨迹**跑，而不是人造脚本；
2. 完全离线、零成本、确定性，适合 CI；
3. 调试线上失败：`moss replay <run_id>` 精确复现；
4. harness 改动的影响可被精确度量——"同样的模型输出，新 harness 的执行结果有没有变"；
5. [spec-07](spec-07-session-artifacts.md) §4.6 的崩溃恢复矩阵需要它当**确定性 oracle**（否则真实 provider 的非确定性会被误判成恢复错误）。

**验收**：录制一次真实的 20 步任务，回放 10 次结果逐字节一致；用回放集替换掉 `SCRIPTED_MODEL_OUTPUTS` 里至少一半的手写脚本；磁带里无明文 secret（扫描断言）。

**风险**

| 风险 | 缓解 |
| --- | --- |
| prompt 改动导致全部磁带失效 | 指纹规范化剔除易变字段；`on_miss=nearest` 在开发期可用；磁带按 `prompt_version` 分目录，重录有脚本 |
| 磁带把 secret 带进仓库 | 落盘前脱敏 + CI 扫描断言 |
| 回放掩盖了真实模型的不确定性 | L1 明确标注"不能证明模型能力"；能力结论只能来自 L2 |

---

## 9.9 实现说明与与 spec 的偏差

八个模块全部落地，测试与 lint 全绿。三处与 spec 字面不同，都是实现时发现原方案
站不住，记在这里而不是悄悄改掉：

1. **§9.2 的工具目录阈值取 16，不是 12。** 内置注册表本身就有 14 个工具，取 12 会让
   **每一次**默认运行都切进目录模式。这个开关是给"接了外部 server 之后工具数膨胀"
   用的，不是用来改默认渲染的。另外光有阈值挡不住线性膨胀——目录段还加了 600 token
   预算并自适应缩短描述，实测 14 → 30 个工具时稳定前缀 1054 → 1147（+8.8%，验收 <20%）。
2. **§9.8 的磁带来源分两档，L1 覆盖 12 个 benchmark 任务里的 10 个。** 引导磁带的模型
   输出仍来自 `SCRIPTED_MODEL_OUTPUTS`，只有**请求指纹**是真的，manifest 里记
   `source: scripted-bootstrap`——它证明的是 harness 合同，不能声称是真实模型轨迹。
   两个任务做不到指纹稳定，登记在 `cassettes.UNCASSETTABLE_TASKS`：一个的 prompt 被预算
   截断（截断点随 workspace 绝对路径长度变化，规范化替得掉路径、替不掉截断位置），
   一个的模型输出**故意**是 secret 形状（磁带落盘必须脱敏，脱完这条任务就没得验了）。
3. **§9.3 的隔离是"AST 白名单 + 进程内受限命名空间 + 沙箱前置"，不是"在沙箱子进程里跑脚本"。**
   spec 里"脚本跑在沙箱进程里"与"每次工具调用逐条走 ToolExecutor"是冲突的——
   ToolExecutor 在主进程里，跨进程回调等于自己造一条 RPC。选择保留后者（护栏不打折），
   沙箱降级成**准入前置**：`sandbox.detect()` 给出 `none` 时根本不暴露 `run_orchestration`。
   相应地白名单做到三层（节点类型 / 属性名 / 自由名字）——只做节点白名单挡不住 `eval(...)`，
   它在 AST 上就是普通的 `Call(Name)`。超时也补了逐行 trace：`while True: pass` 过得了
   白名单（它不是逃逸，是死循环），只靠 `join(timeout)` 会留一个烧满一个核的 daemon 线程。

验收对照：

| 模块 | 验收 | 结果 |
| --- | --- | --- |
| 9.1 | 子 agent 能力是父集的严格子集 | 断言测试；`context_seed` token 量 < history 的 60% |
| 9.2 | 工具数 30 时 prefix 增长 <20% | +8.8%；越权外部工具 fail-closed 拒绝 |
| 9.3 | AST 白名单拒绝 ≥20 条逃逸样例 | 32 条逐条测试 |
| 9.4 | skill 数 20 时 prefix 增量 <400 token | 卡在预算内；越权 `allowed-tools` 被拒；篡改触发确认 |
| 9.5 | 超时不阻断、退出码 2 拒绝并记事件、agent 写不进 `.moss/hooks/` | 三条各有测试 |
| 9.6 | 25 步 run 的 HTML <500KB、离线、无外部请求 | 远低于上限；正则扫外部引用 |
| 9.7 | aux 失败回落主模型并记事件 | 有测试；成本下降 ≥25% 的配对评测要真实后端，未跑 |
| 9.8 | 回放 10 次逐字节一致；替换 ≥半数手写脚本；磁带无明文 secret | 10/12 任务；两条扫描断言 |

**未验证的部分**（需要真实后端和成本，不在 CI 里）：9.1 的"父 context token 下降 ≥40% 且
结论质量不降（judge 判定）"、9.7 的"总成本下降 ≥25% 且 pass_rate 不下降（配对检验）"、
9.2 的"接一个真实 MCP server 跑通"、9.8 的"录制一次真实的 20 步任务"。这些都是 L2 级证据，
按 [spec-08](spec-08-evaluation.md) 的分层纪律，不能拿 L1 的结果代替。

---

## 10. 跨模块的实施顺序

| 阶段 | 模块 | 理由 |
| --- | --- | --- |
| 阶段二 | 9.8 录制回放 | [spec-08](spec-08-evaluation.md) 的 L1 与 [spec-07](spec-07-session-artifacts.md) 的崩溃矩阵都依赖它 |
| 阶段三 | 9.1 sub-agent、9.4 Skills、9.7 多模型路由 | 都是 P1，且各自独立、可并行推进 |
| 阶段四 | 9.2 MCP、9.5 Hooks、9.6 可视化 | 生态与体验，成本较高 |
| 阶段四（有沙箱后） | 9.3 code mode | **硬前置是沙箱**，没有就不做 |

## 11. 共同的开放问题

1. MCP 与 Skills 在"外部能力接入"上功能重叠，是否该统一成一套注册模型？倾向：先各自实现，等两边都跑起来再看要不要合并 `ToolSpec` 的来源字段。
2. aux model 的失败是否该计入主 run 的失败？倾向：不计，但要记 `aux_degraded=True` 进 report，评测里作为切片维度。
3. 磁带要不要进 git？倾向：小磁带（<1MB）进，大的走 `benchmarks/cassettes/.gitignore` + 本地生成脚本。
4. sub-agent 的 `findings` 要不要直接写进记忆？倾向：不直接写，trust 只能是 `tool` 级，走 [spec-05](spec-05-memory.md) 的正常准入。
