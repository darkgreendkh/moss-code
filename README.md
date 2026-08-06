# moss

`moss` 是一个零第三方运行时依赖的本地 coding agent——一个包在模型外面的控制循环，
负责组 prompt、解析模型输出、校验并执行工具、写 trace/report、维护跨轮记忆。

它在指定工作区内读取、修改和验证代码，把会话与每次运行的审计工件保存在本地 `.moss/`。
贯穿全项目的一条原则是：**模型的输出是"申请"，不是"命令"**——
从解析到落盘的每一环都能拒绝、能留痕、能回退。

## 安装与启动

需要 Python 3.10+。

```bash
uv sync
uv run moss                                    # REPL
uv run moss "排查测试失败并给出修复方案"          # one-shot
uv run moss --cwd /path/to/repo
pip install -e .                               # 装完可直接用 `moss`
```

默认 provider 是 DeepSeek。密钥写进本地 `.env`（模板见 `.env.example`）；
配置优先级是 `显式 CLI 参数 > .env 里的 MOSS_* > 旧环境变量 > 代码默认值`。

| provider | 默认模型 | 协议 |
| --- | --- | --- |
| `deepseek` | `deepseek-v4-pro` | Anthropic-compatible Messages |
| `openai` | `gpt-5-5` | OpenAI-compatible Responses |
| `anthropic` | `claude-opus-5` | Anthropic-compatible Messages |
| `ollama` | `qwen3:8b` | Ollama Generate |

## 一次运行发生了什么

```
cli.py (装配 / REPL / 进度渲染)
  └─ runtime.py::Moss  (facade：所有状态与护栏挂在这里)
       └─ agent_loop.py::AgentLoop.run   感知 → 决策 → 行动 → 记录
            ├─ context_manager   按预算组 prompt（六段）+ admission gate
            ├─ providers/        统一 complete()（三种协议）
            ├─ output_parser     模型输出 → 动作 / 最终答案
            ├─ tool_executor     执行护栏 → tools（显式白名单）
            ├─ compaction        上下文压缩：可逆 / 幂等 / 闭合
            ├─ action_ledger     risky 动作的 intent / receipt 两阶段
            └─ run_store         .moss/runs/<id>/{task_state,trace,report}
```

一轮可以有多个动作；只读工具批可并发（默认关闭），risky 恒串行。
**中断、超预算、装不下、后端报错——每一条退出路径都留下完整工件。**

详见 [docs/architecture.md](docs/architecture.md)。

## 安全与持久化

- **路径锚定**：文件工具经 `Moss.path()`，resolve 后必须在 workspace root 之下；不跟随符号链接目录。
- **能力标签**：`fs_read` / `fs_write` / `exec` / `network` / `spawn` / `memory_write`，
  可用 `--allow` / `--deny` 叠加 glob 作用域。risky 但未声明能力的工具**直接拒绝**。
- **shell 分级**：基于 shlex 的结构化解析，六档；`denied` 连审批都不给，判不出来的一律按最坏算。
- **审批**：只展示摘要（写文件给脱敏 diff，shell 给风险分级），
  审批与写入之间用 `expected_sha` 挡 TOCTOU。
- **沙箱**：策略层 / `sandbox-exec`·`bwrap` / 容器三层，**任何降级都进 report 且打 stderr**。
- **脱敏**：所有落盘或展示的文本先过 `redact_artifact`。
- **原子写**：临时文件 → fsync → `os.replace` → fsync 目录。两个 fsync 缺一不可。
- **可回退**：risky 动作留 undo 快照，`/rewind` 同时回退文件、历史和记忆；
  用户自己动过的文件绝不悄悄覆盖。

每次运行写入 `.moss/runs/<run_id>/`，会话写入 `.moss/sessions/`；`.moss/` 整体被 Git 忽略。
见 [docs/features/tool-safety.md](docs/features/tool-safety.md) 与
[docs/reference/storage.md](docs/reference/storage.md)。

## 扩展点

全部零第三方依赖、默认关闭或行为不变，开关见 `moss --help`。

| 能力 | 开关 | 一句话 |
| --- | --- | --- |
| 录制回放 | `--record DIR` / `--replay DIR` | 把真实模型调用录成磁带，之后离线、零成本、逐字节可复现地重跑 |
| 多模型路由 | `--aux-model` / `--aux-provider` | compaction、反思、judge 这些脏活走便宜模型，主线不变 |
| MCP 客户端 | `.moss/config.json` 的 `mcp.servers` | 外部工具在启动期落进白名单，必须声明 capabilities |
| MCP 服务端 | `moss mcp serve` | 把 moss 的只读工具暴露给别的 agent，走同一套护栏 |
| Skills | `.moss/skills/*.md` | 三级渐进披露；`allowed-tools` 临时收紧能力；第三方 skill 改动要确认 |
| Hooks | `.moss/hooks/<point>` | `pre_tool`（退出码 2 可拒绝）/ `post_tool` / `pre_final` / `post_run` |
| 子 agent | `delegate` 工具 | 结构化背景进、带证据锚点的结论出；能力必须是父集的子集 |
| trace 可视化 | `moss runs show <id> --html` | 单文件、内联 CSS/SVG、零外部请求 |
| code mode | `--enable-code-mode` | 一段受限 Python 批量编排只读工具调用；**沙箱是硬前置** |

外部能力一律 **fail-closed**：声明不全、前置条件不满足就拒绝启用，不静默降级。
理由见 [docs/decisions/0002-fail-closed-extensions.md](docs/decisions/0002-fail-closed-extensions.md)。

## 评测：L0–L4

评测证据严格分层：L0 不变量、L1 scripted/回放合同、L2 真实模型能力、L3 对抗安全、L4 成本效用。
**L1 的通过率只说明 harness 按给定动作工作，不代表模型能力**；
L2/L3 必须带 manifest、成本、统计区间和明确限制。

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
uv run python scripts/eval_lint_tasks.py benchmarks/tasks/mined
uv run python scripts/eval_lint_adversarial.py
```

当前证据边界（含**尚未产生**的部分）写在
[docs/features/evaluation.md](docs/features/evaluation.md)。

## 文档

| 入口 | 内容 |
| --- | --- |
| [docs/README.md](docs/README.md) | 文档总索引 |
| [docs/architecture.md](docs/architecture.md) | 整体架构、模块关系、核心运行链路 |
| [docs/features/](docs/features/) | 七份功能文档：主循环、仓库上下文、工具安全、提示词与上下文、记忆、会话与运行工件、评测 |
| [docs/reference/](docs/reference/) | 配置项、CLI、落盘结构的精确速查 |
| [docs/decisions/](docs/decisions/) | 重要技术决策及其理由 |
| [docs/plans/](docs/plans/) | 未完成的方案（active）与已落地的原稿（archive） |
| [docs/specs/](docs/specs/) | 九份分模块设计稿（含验收标准） |

## License

[MIT](LICENSE)
