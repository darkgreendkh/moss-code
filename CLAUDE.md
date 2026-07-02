# CLAUDE.md

moss 是一个轻量本地 coding agent：一个包在模型外面的控制循环，负责组 prompt、解析模型输出、校验并执行工具、写 trace/report、维护跨轮记忆。目标是"轻量好用"，刻意保持零第三方运行时依赖（HTTP 用 stdlib `urllib`，`.env`/frontmatter 都是手写解析器，不引入 requests/YAML）。

## 常用命令

```bash
python -m pytest tests/ -q        # 全量测试，约 4 分钟
python -m pytest tests/test_moss.py -q -k "pattern"   # 单点调试
pip install -e .                  # 安装后可直接用 `moss` 命令
python -m moss                    # 模块入口（等价于 moss）
uv run ruff check moss tests scripts   # lint（本机 base 环境没装 ruff，用 uv）
```

**测试基线（Windows 本机）：152 passed / 7 failed 即为绿色。** 那 7 个失败全部是环境问题，不是代码 bug，只需要关注新增失败：
- `test_evaluator.py` ×3 + `test_allowed_tools.py::test_benchmark_evaluator_*`：benchmark verifier 需要真实环境/`python3`
- `test_safety_invariants.py::test_run_shell_uses_allowlisted_environment_only`：测试用 POSIX `shlex.quote`，cmd.exe 解析不了
- `test_moss.py::test_trace_and_report_redact_secret_env_values`：测试命令用 `printf`，不是 cmd.exe 内置命令
- `test_safety_invariants.py::test_symlink_path_traversal_is_rejected`：Windows 创建符号链接需要特权（WinError 1314）

Git 约定：个人项目，直接提交并 push 到 `main`，不开分支/PR。push 偶发 TLS 报错，重试即可。

## 架构地图

一次 `ask()` 的数据流：

```
cli.py (装配/REPL/进度渲染)
  └─ runtime.py::Moss (facade：所有状态和护栏都挂在这里)
       └─ agent_loop.py::AgentLoop.run (感知→决策→行动→记录 主循环)
            ├─ context_manager.py   每轮按预算组 prompt（prefix/memory/relevant/history/request 五段）
            ├─ providers/clients.py 统一 complete() 接口（Ollama / OpenAI /responses / Anthropic /messages）
            ├─ runtime.py::parse    模型输出 → ("tool"|"final"|"retry", payload)
            ├─ tool_executor.py     执行护栏：allowlist→存在性→校验→重复检测→审批→快照 diff
            │    └─ tools.py        工具白名单（显式注册，非动态发现）
            ├─ checkpoint.py        每步落 checkpoint（上限 CHECKPOINT_HISTORY_LIMIT=40，自动裁剪）
            └─ run_store.py         .moss/runs/<run_id>/{task_state.json, trace.jsonl, report.json}
```

支撑模块：
- `workspace.py`：仓库快照（git 状态 + 白名单文档），进 prompt prefix；`MAX_TOOL_OUTPUT=16000`、`MAX_HISTORY=32000` 在这里
- `prompt_prefix.py`：稳定前缀构建。**prompt cache key 用 `stable_hash`（只覆盖身份/规则/Tools/Skills 段），不用整段 hash**——否则 agent 自己写文件会导致 workspace 段变化、缓存键每轮抖动
- `features/memory.py`：分层记忆（working / episodic notes / durable topics），文件摘要带 freshness 失效
- `session_store.py`：会话持久化到 `.moss/sessions/`；delegate 子 agent 的会话隔离在 `.moss/delegates/`（不能污染 `--resume latest`）
- `security.py`：secret 检测/脱敏；`run_shell` 只继承 `DEFAULT_SHELL_ENV_ALLOWLIST` 里的环境变量（**含 Windows 必需的 COMSPEC/SYSTEMROOT 等，删了 run_shell 在 Windows 上直接崩**）
- `config.py`：`.env` 加载。坏行跳过并警告，不允许让整个启动崩掉
- `skills.py`：`.moss/skills/*.md`（frontmatter: name/description，正文按 `use_skill` 懒加载）
- `evaluation/`：benchmark 与 ablation，不属于运行时路径

公共 API 只从 `moss/__init__.py` 导出；旧的 `moss.evaluator`/`moss.metrics`/`moss.models`/`moss.memory` 平铺模块已删除，不要复活它们。

## 关键约定与不变量

1. **持久化必须原子写**：先写同目录临时文件再 `os.replace`（见 `SessionStore.save` / `RunStore._write_json_atomic`）。session 每次 record 都整份重写，非原子写会在断电/Ctrl-C 时丢整个会话。
2. **CLI 输出契约**：最终答案走 stdout（可管道），进度/警告/错误走 stderr。进度通过 `agent.progress_observer` 钩子（`emit_progress`），observer 异常必须被吞掉，绝不影响控制流。
3. **错误收敛，不裸抛**：模型后端异常由 `AgentLoop._finish_model_error` 收敛为已收尾的失败运行（task_state=failed / stop_reason=model_error，trace+report 齐全），并对错误信息脱敏。one-shot 模式下失败必须非零退出（CI 依赖这个）。`KeyboardInterrupt` 继承 BaseException 不会被捕获——REPL 里 Ctrl-C 只取消当前轮。
4. **所有落盘/展示的文本先过脱敏**：`redact_artifact` / `redact_text`，secret 名单来自 `DEFAULT_SECRET_ENV_NAMES` + `MOSS_SECRET_ENV_NAMES` + `--secret-env-name`。
5. **路径锚定**：所有文件类工具经 `Moss.path()`，resolve 后必须在 workspace root 之下（防 `../` 和符号链接逃逸）。遍历工作区时不跟随符号链接目录（防死循环）。
6. **工具是显式注册的白名单**（`BASE_TOOL_SPECS`），risky 工具走审批（`ask`/`auto`/`never`）；审批提示只展示摘要（`_approval_summary`），不 dump 完整 args。
7. **快照 diff 用 `(mtime_ns, size)`**，不做内容 hash——risky 工具每次调用前后各扫一遍工作区，性能敏感。
8. **注释风格**：中文、解释"为什么存在/在链路里的位置"，新代码保持一致。

## 配置

优先级：`显式 CLI 参数 > .env 里的 MOSS_* > 旧环境变量 > 代码默认值`。`.env` 在构建 provider client 前加载并覆盖进程环境变量（解析器只读字面量，不支持 `$VAR` 展开）。

默认 provider 是 `deepseek`。各 provider 真实默认模型（改动时必须同步 `cli.py`、`evaluation/metrics.py`、`.env.example`、README 和相关测试断言，历史上这里出现过四处编造的模型名）：

| provider | 默认模型 | 协议 |
| --- | --- | --- |
| deepseek | `deepseek-chat` | Anthropic-compatible `/messages` |
| openai | `gpt-4o` | OpenAI-compatible `/responses` |
| anthropic | `claude-sonnet-4-5-20250929` | Anthropic-compatible `/messages` |
| ollama | `qwen3:8b` | Ollama `/api/generate` |

CLI 默认：`--max-steps 25`、`--max-new-tokens 4096`、`--approval ask`。ContextManager 预算以**估算 token** 为单位（见 `token_budget.py`，CJK 约 1 char/token、拉丁约 4 chars/token；测试可注入 `measure=len` 走字符级以稳定断言）：总预算 12000 token（section 预算 prefix 3000 / memory 1000 / relevant 800 / history 6000）。

## 本地状态目录（全部 gitignored）

```
.moss/sessions/    用户会话（--resume latest 按 mtime 取最新）
.moss/delegates/   delegate 子 agent 的一次性会话（隔离，防污染 latest）
.moss/runs/<id>/   单次运行审计工件：task_state.json / trace.jsonl / report.json
.moss/skills/      技能 markdown
.env               本地密钥（仓库只保留 .env.example）
```
