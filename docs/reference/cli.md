# CLI 参考

```bash
moss                                  # REPL
moss "排查测试失败并给出修复方案"        # one-shot
moss --cwd /path/to/repo              # 指定工作区
python -m moss                        # 模块入口，等价于 moss
```

**输出契约**：最终答案走 **stdout**（可管道），进度/警告/错误走 **stderr**。
one-shot 失败必须**非零退出**——CI 依赖这个。

---

## 1. 主命令参数

### 工作区与后端

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `prompt`（位置参数，可多个） | 无 | 给了就是 one-shot，不给进 REPL |
| `--cwd` | `.` | 工作区目录 |
| `--provider` | `MOSS_PROVIDER` 或 `deepseek` | `ollama` / `openai` / `anthropic` / `deepseek` |
| `--model` | 见 [configuration](configuration.md#2-provider) | 模型名覆盖 |
| `--base-url` | provider 默认 | deepseek / openai / anthropic 的 API base |
| `--host` | `http://127.0.0.1:11434` | Ollama 服务地址 |
| `--ollama-timeout` | `300` | 秒 |
| `--openai-timeout` | `300` | 秒 |

### 采样与步数

| 参数 | 默认 |
| --- | --- |
| `--max-steps` | `25` |
| `--max-new-tokens` | `4096` |
| `--temperature` | `0.2` |
| `--top-p` | `0.9` |

### 多维预算（默认全 None = 老行为）

| 参数 | 说明 |
| --- | --- |
| `--max-input-tokens N` | 输入 token 上限 |
| `--max-output-tokens N` | 输出 token 上限 |
| `--max-seconds S` | 墙钟时间上限 |
| `--max-usd U` | 估算花费上限（未知价格记 `null`，**不当成 0**） |

软阈值 80% 提醒收敛，硬阈值不再调模型直接优雅收尾。

### 安全

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--approval` | `ask` | `ask` / `auto` / `never` |
| `--sandbox` | `auto` | `off` / `auto` / `sandbox-exec` / `bwrap` / `docker` / `podman` |
| `--allow CAP[=GLOBS]` | — | 可重复。把能力限制在 glob 范围内 |
| `--deny CAP[=GLOBS]` | — | 可重复。整体或按路径禁用能力 |
| `--allow-network HOSTS` | 空 | 网络命令的 host 白名单；空 = 不做 host 过滤 |
| `--injection-scan` | `on` | 命中只收紧策略（剩余 risky 强制审批），不拒绝执行 |
| `--secret-env-name NAME` | — | 可重复。额外脱敏的环境变量名 |

能力：`fs_read` `fs_write` `exec` `network` `spawn` `memory_write`。

### 主循环

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--parallel-tools` | `off` | 只读工具批并发（上限 4）；risky 恒串行 |
| `--verify-before-final` | `on` | 改了文件却没跑验证时拦一次（只拦一次） |
| `--reflect` | `rule` | `off` / `rule` / `model`。run 结束时蒸馏 procedural memory |

### 提示词与上下文

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--tool-protocol` | `auto` | `auto` / `native` / `text` |
| `--context-mode` | `rerender` | `rerender` / `append_only` |
| `--compaction` | `off` | `off` / `rule` / `model`。off 是消融基线 |
| `--no-prompt-cache` | 关 | 本进程禁用 provider 的缓存字段 |

### 恢复

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--resume ID\|latest` | — | 恢复会话 |
| `--resume-parts` | `all` | `memory` / `plan` / `history` / `checkpoint` 的逗号子集 |
| `--explain` | 关 | 打印恢复会带回什么、会不会重放有副作用的动作，**然后退出** |
| `--fork CHECKPOINT_ID` | — | 从 checkpoint 分叉出新会话，原会话不动 |

### 扩展点（spec-09，全部默认关闭或行为不变）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--record DIR` | — | 把每次模型调用录成磁带（落盘前脱敏） |
| `--replay DIR` | — | 从磁带回放，不调 provider |
| `--replay-on-miss` | `fail` | `fail` / `nearest` / `passthrough` |
| `--aux-model` | — | 脏活（compaction / 反思 / judge）走的便宜模型 |
| `--aux-provider` | 主 provider | aux 模型的 provider |
| `--enable-code-mode` | 关 | 暴露 `run_orchestration`；**需要可用沙箱，否则即使开了也不暴露** |

MCP server 与 `catalog_threshold` 写在 `.moss/config.json`（结构化配置）；
用户钩子放在 `.moss/hooks/<point>`（需可执行位）。

---

## 2. `moss runs`

```bash
moss runs list [--limit 20] [--cwd DIR]
moss runs show <run_id> [--html]
moss runs verify [<run_id>]
moss runs prune [--keep-count N] [--keep-days D] [--dry-run]
moss runs pin <run_id> [--off]
moss runs export <run_id> [--otel]
```

| 子命令 | 说明 |
| --- | --- |
| `list` | 从 `.moss/runs/index.jsonl` 读，不 glob 全部 run 目录 |
| `show` | 打印 task state 与 report；`--html` 输出单文件页面（内联 CSS/SVG，**零外部请求**，工具输出全部转义） |
| `verify` | 逐条校验 trace 哈希链；**被改过返回非零**。省略 run_id 校验全部 |
| `prune` | 过期 run 打包成 `<run_id>.jsonl.gz`（逐文件 jsonl，仍可 grep）。不给参数用 `MOSS_RUN_RETENTION_*` |
| `pin` | 钉住一个 run，保留策略永不清理 |
| `export` | trace 导出到 stdout；`--otel` 输出 OTLP/JSON（stdlib 生成，只落文件不推 collector） |

---

## 3. `moss memory`

```bash
moss memory list [--scope project|global] [--cwd DIR]
moss memory show <id>
moss memory add "文本" --topic TOPIC [--tag TAG]...
moss memory forget <id>
moss memory export
```

`--scope project`（默认）读 `<workspace>/.moss/memory/`，
`--scope global` 读 `~/.moss/memory/`。
`forget` 是追加 tombstone，不是抹掉记录。

---

## 4. `moss mcp`

```bash
moss mcp serve [--cwd DIR] [--tools read_file,list_files,search_text]
```

把 moss 的工具暴露成 MCP server（JSON-RPC 2.0 over stdio）。
**risky 工具无论怎么指定都会被丢掉**，默认只导出只读工具。
server 侧走 `Moss.execute(ActionRequest)` 这个唯一入口——
外部 agent 调进来的工具和模型自己调的走同一套护栏。

---

## 5. REPL 斜杠命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 帮助 |
| `/memory` | 显示蒸馏出来的 working memory |
| `/session` | 显示当前会话目录路径 |
| `/reload` | 从磁盘重新加载工具与技能 |
| `/rewind [n]` | 回退最近 n 个改文件的步骤（**文件 + history + memory 一起**） |
| `/rewind!` | 强制回退，越过"用户自己动过这些文件"的保护 |
| `/reset` | 清空当前会话的历史与记忆 |
| `/exit` | 退出 |

`Ctrl-C` 取消当前任务并回到提示符（`KeyboardInterrupt` 继承 `BaseException`，
不会被错误处理吞掉；run 工件仍然完整落盘）。

---

## 6. 工具清单（模型可用）

| 工具 | risky | 参数 |
| --- | --- | --- |
| `list_files` | | `path=.` |
| `read_file` | | `path`, `start=1`, `end=300`（输出头报 `(lines x-y of N)`） |
| `search_text` | | `pattern`, `path=.` |
| `read_artifact` | | `path`, `start=1`, `end=200`（scope 限本 run 目录） |
| `update_plan` | | `steps`（`{id, title, status}` 列表） |
| `write_file` | ✅ | `path`, `content` |
| `edit_file` | ✅ | `path`, `old_text`, `new_text` |
| `run_shell` | ✅ | `command`, `timeout=60`（1–600） |
| `memory_write` | | `scope`, `topic`, `text`, `tags=[]` |
| `memory_update` | | `id`, `text` |
| `memory_delete` | | `id` |
| `memory_search` | | `query`, `limit=5`（1–20） |
| `delegate` | | `task` 或 `tasks`, `max_steps=3`, `focus=[]` |
| `use_skill` | | `name` |
| `describe_tool` | | `name`（工具目录模式下取完整 schema） |
| `run_orchestration` | ✅ | `script`（需 `--enable-code-mode` + 沙箱） |

工具是**显式注册的白名单**（`BASE_TOOL_SPECS`），不做动态发现。
Tools 段超过 `catalog_threshold`（默认 16）时切成"目录 + `describe_tool` 按需取 schema"。

---

## 7. 开发常用命令

```bash
uv run --with pytest python -m pytest tests/ -q
python -m pytest tests/test_moss.py -q -k "pattern"   # 单点调试
uv run ruff check moss tests scripts                  # base 环境没装 ruff，用 uv
pip install -e .                                      # 安装后可直接用 moss
```
