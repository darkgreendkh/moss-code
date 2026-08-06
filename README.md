# moss

`moss` 是一个零第三方运行时依赖的本地 coding agent。它在指定工作区内读取、修改和验证代码，把会话与每次运行的审计工件保存在本地 `.moss/` 目录。

## 安装与启动

需要 Python 3.10+。

```bash
uv sync
uv run moss
uv run moss "排查测试失败并给出修复方案"
uv run moss --cwd /path/to/repo
```

默认 provider 是 DeepSeek。密钥写入本地 `.env`；配置优先级是 `显式 CLI 参数 > .env 里的 MOSS_* > 旧环境变量 > 代码默认值`。

| provider | 默认模型 | 协议 |
| --- | --- | --- |
| `deepseek` | `deepseek-v4-pro` | Anthropic-compatible Messages |
| `openai` | `gpt-5-5` | OpenAI-compatible Responses |
| `anthropic` | `claude-opus-5` | Anthropic-compatible Messages |
| `ollama` | `qwen3:8b` | Ollama Generate |

## 安全与持久化

文件工具被锚定在 workspace root，危险操作经过审批与能力检查，输出和工件在落盘前脱敏。每次运行写入 `.moss/runs/<run_id>/`，会话写入 `.moss/sessions/`；二者均被 Git 忽略。

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
| trace 可视化 | `moss runs show <id> --html` | 单文件、内联 CSS/SVG、零外部请求 |
| code mode | `--enable-code-mode` | 一段受限 Python 批量编排只读工具调用；**沙箱是硬前置** |

设计见 [docs/specs/spec-09-new-modules.md](docs/specs/spec-09-new-modules.md)。

## 评测：L0–L4

评测证据严格分层：L0 不变量、L1 scripted 合同、L2 真实模型能力、L3 对抗安全、L4 成本效用。L1 的通过率只说明 harness 按给定动作工作，不代表模型能力；L2/L3 必须带 manifest、成本、统计区间和明确限制。

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
uv run python scripts/eval_lint_tasks.py benchmarks/tasks/mined
uv run python scripts/eval_lint_adversarial.py
```

完整操作、审计与证据边界见 [docs/evaluation.md](docs/evaluation.md)，设计见 [docs/specs/spec-08-evaluation.md](docs/specs/spec-08-evaluation.md)。

## License

[MIT](LICENSE)
