# 配置参考

配置分三处，按用途而不是按习惯放：

| 位置 | 放什么 | 为什么 |
| --- | --- | --- |
| CLI 参数 | 这一次运行的行为 | 一次性，不该留在文件里 |
| `.env` | 密钥与扁平的默认值 | 手写解析器只读字面量 |
| `.moss/config.json` | **结构化**配置（MCP servers、doc 名单） | 嵌套结构塞不进 `.env` |

---

## 1. 优先级

```
显式 CLI 参数  >  .env 里的 MOSS_*  >  旧环境变量  >  代码默认值
```

`.env` 在构建 provider client **之前**加载，并覆盖进程环境变量。

解析器的两条限制（刻意的，见
[decisions/0001-zero-dependencies.md](../decisions/0001-zero-dependencies.md)）：

- 只读字面量，**不支持 `$VAR` 展开**。
- 坏行**跳过并警告**，不让整个启动崩掉。

"旧环境变量"指没有 `MOSS_` 前缀的通用名（`OPENAI_API_KEY` 等），
由 `config.provider_env(name, legacy_names)` 兜底。

---

## 2. Provider

默认 provider 是 **`deepseek`**。

| provider | 默认模型 | 协议 |
| --- | --- | --- |
| `deepseek` | `deepseek-v4-pro` | Anthropic-compatible `/messages` |
| `openai` | `gpt-5-5` | OpenAI-compatible `/responses` |
| `anthropic` | `claude-opus-5` | Anthropic-compatible `/messages` |
| `ollama` | `qwen3:8b` | Ollama `/api/generate` |

> 改动默认模型时**必须同步**：`moss/cli.py`、`moss/evaluation/metrics.py`、
> `.env.example`、`README.md`、`CLAUDE.md` 和相关测试断言。
> 历史上这里出现过四处互相矛盾的编造模型名。

| 环境变量 | 旧名兜底 | 说明 |
| --- | --- | --- |
| `MOSS_PROVIDER` | | 默认 provider |
| `MOSS_DEEPSEEK_API_BASE` / `_API_KEY` / `_MODEL` | `DEEPSEEK_API_KEY` | 默认 base `https://api.deepseek.com/anthropic` |
| `MOSS_OPENAI_API_BASE` / `_API_KEY` / `_MODEL` | `OPENAI_API_KEY` | 默认 base `https://api.openai.com/v1` |
| `MOSS_ANTHROPIC_API_BASE` / `_API_KEY` / `_MODEL` | `ANTHROPIC_API_KEY` | 默认 base `https://api.anthropic.com/v1` |

Ollama 走 `--host`（默认 `http://127.0.0.1:11434`），不需要 key。

---

## 3. 环境变量总表

### 仓库上下文

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MOSS_REPO_MAP` | `on` | repo map 总开关；`off` 一键回到没有它的 prefix（消融基线） |
| `MOSS_REPO_MAP_BUDGET` | `800` | repo map 的 token 预算 |
| `MOSS_SNAPSHOT_STRATEGY` | `auto` | `git`（只扫变更集）/ `walk`（全量）/ `auto` |
| `MOSS_SNAPSHOT_EXCLUDE` | 空 | 额外排除的 glob，逗号分隔 |

### 上下文预算

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MOSS_CONTEXT_RATIO` | `0.5` | 模型窗口的利用比例 |
| `MOSS_CONTEXT_HARD_CAP` | `60000` | 总预算硬上限（token） |

总预算 = `min(context_window * ratio, hard_cap) - max(max_new_tokens, 1024)`。
**认不出来的模型退回历史值 12000**，不拿猜出来的窗口去放大预算。

### 记忆

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MOSS_MEMORY_V2` | `on` | 关掉退回 legacy durable store |
| `MOSS_MEMORY_DECAY_DAYS` | `7` | 召回的时间衰减半衰期（天） |

### run 工件保留

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MOSS_RUN_RETENTION_COUNT` | `200` | 保留最近 N 个 run |
| `MOSS_RUN_RETENTION_DAYS` | `30` | 保留最近 D 天 |

两个维度是**"或"**关系；设 `0` 关掉那一维。
**永不清理**：pinned / 持有有效租约 / 被 `artifacts/*.json` 引用的 run。

### 安全

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MOSS_SECRET_ENV_NAMES` | 空 | 额外需要脱敏的环境变量名，逗号分隔 |

内置名单：`MOSS_OPENAI_API_KEY` / `OPENAI_API_KEY` / `OPENAI_API_TOKEN` /
`MOSS_ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` /
`MOSS_DEEPSEEK_API_KEY` / `DEEPSEEK_API_KEY` / `GITHUB_PAT` / `GH_PAT`。
再叠加 `--secret-env-name`（可重复）。

---

## 4. `.moss/config.json`

结构化配置，**不进 `.env`**。目前两个段：

```json
{
  "repo_context": {
    "doc_names": ["README.md", "AGENTS.md", "CLAUDE.md"]
  },
  "mcp": {
    "catalog_threshold": 16,
    "servers": {
      "fs": {
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
        "capabilities": ["fs_read"],
        "tools": ["read_file"]
      }
    }
  }
}
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `repo_context.doc_names` | 内置 `DOC_NAMES` | **整体覆盖**文档发现名单，不是追加 |
| `mcp.catalog_threshold` | `16` | Tools 段超过这么多条就切成目录 + `describe_tool` |
| `mcp.servers.<name>.command` | — | stdio 启动命令 |
| `mcp.servers.<name>.capabilities` | — | **必填**。缺声明 fail-closed 直接拒绝注册 |
| `mcp.servers.<name>.tools` | 全部 | 只导入这几个工具 |

外部工具一律额外加隐含的 `network` 能力并标 risky。

---

## 5. CLI 开关分组

完整参数见 [cli.md](cli.md)。这里按"这个开关改变什么"分组：

### 默认改变行为

| 开关 | 默认 |
| --- | --- |
| `--max-steps` | `25` |
| `--max-new-tokens` | `4096` |
| `--approval` | `ask` |
| `--sandbox` | `auto` |
| `--injection-scan` | `on` |
| `--verify-before-final` | `on` |
| `--tool-protocol` | `auto` |
| `--context-mode` | `rerender` |
| `--reflect` | `rule` |
| `--temperature` / `--top-p` | `0.2` / `0.9` |

### 默认关闭 / 不设即老行为

| 开关 | 默认 | 说明 |
| --- | --- | --- |
| `--parallel-tools` | `off` | |
| `--compaction` | `off` | off 就是纯截断行为，也是消融基线 |
| `--max-input-tokens` / `--max-output-tokens` / `--max-seconds` / `--max-usd` | `None` | 不设只有 `--max-steps` 生效 |
| `--enable-code-mode` | 关 | **沙箱不可用时即使开了也不暴露 `run_orchestration`** |
| `--aux-model` / `--aux-provider` | `None` | 不设即全部回落主模型，行为**逐字节一致** |
| `--record` / `--replay` | `None` | |
| `--replay-on-miss` | `fail` | CI 默认 |
| `--no-prompt-cache` | 关 | |

### 安全作用域

```bash
moss --allow fs_write=src/**,tests/**   # 把能力限制在 glob 范围内
moss --deny network                     # 整体禁掉一个能力
moss --deny fs_write=.github/**         # 按路径禁
moss --allow-network api.example.com    # 网络命令的 host 白名单
```

能力共六个：`fs_read` `fs_write` `exec` `network` `spawn` `memory_write`。
内置 `fs_write` 默认拒绝：`.git/**` `.github/**` `.env` `.env.*` `.moss/**`。

---

## 6. 其它可覆盖的东西

| 路径 | 作用 |
| --- | --- |
| `.moss/prompts/system.md` | 覆盖内置 `p1` 稳定 system head；版本记为 `file:<sha256前12位>` 并写进 report / run_manifest |
| `.moss/skills/*.md` | 技能（frontmatter：`name` / `description` / `allowed-tools` / `scope` / `resources` / `source`） |
| `.moss/hooks/<point>` | 用户钩子，**需要可执行位**；`pre_tool` / `post_tool` / `pre_final` / `post_run` |

---

## 7. 代码里的关键常量

改这些要连带看对应模块的注释——它们大多不是随手取的数。

| 常量 | 值 | 位置 |
| --- | --- | --- |
| `ARTIFACT_THRESHOLD` | = `MAX_TOOL_OUTPUT`（16000 字符） | `tool_executor.py` |
| `MAX_TOOL_OUTPUT` / `MAX_HISTORY` | 16000 / 32000 | `token_budget.py` |
| `CHECKPOINT_HISTORY_LIMIT` | 40 | `checkpoint.py` |
| `TOOL_CATALOG_THRESHOLD` | 16 | `prompt_prefix.py` |
| `SKILLS_PREFIX_BUDGET_TOKENS` | 400 | `skills.py` |
| `GIT_FACTS_TTL_S` | 0.5 秒 | `workspace.py` |
| `DOC_PREVIEW_BUDGET` | 1200 字符 | `workspace.py` |
| 租约 TTL | 90 秒 | `lease.py` |
| 钩子超时 | 3 秒 | `hooks.py` |
| 并发只读工具上限 | 4 | `agent_loop.py` |
| 停滞窗口 | 8 步 | `stall.py` |
| `TRACE_SCHEMA_VERSION` | 2 | `trace_events.py` |
| `WORKSPACE_FINGERPRINT_VERSION` | `ws-v2` | `workspace.py` |
