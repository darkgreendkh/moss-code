# moss 文档

moss 是一个零第三方运行时依赖的本地 coding agent：一个包在模型外面的控制循环，
负责组 prompt、解析模型输出、校验并执行工具、写 trace/report、维护跨轮记忆。

这里是文档总入口。四类文档各有分工，**先按"你现在想干什么"选**：

| 你想…… | 去哪 |
| --- | --- |
| 搞懂整体是怎么跑起来的 | [architecture.md](architecture.md) |
| 搞懂某一块功能的设计与取舍 | [features/](#功能文档) |
| 查一个配置项/命令/落盘路径的准确值 | [reference/](#速查参考) |
| 知道某个设计"为什么是这样" | [decisions/](#技术决策) |
| 知道还有什么没做完 | [plans/active/](plans/active/) |
| 看某个模块当初的完整设计稿 | [specs/](#原始设计稿specs) |

---

## 功能文档

每份对应一条完整的功能链路，包含设计意图、关键不变量和相关代码位置。

| 文档 | 覆盖 |
| --- | --- |
| [features/agent-loop.md](features/agent-loop.md) | 主循环：感知→决策→行动→记录、一轮多动作、停滞检测、预算、收尾自检、中断与错误收敛 |
| [features/repo-context.md](features/repo-context.md) | 仓库上下文：git 事实、分层项目文档、repo map 与起点锚、工作区快照 |
| [features/tool-safety.md](features/tool-safety.md) | 工具安全：白名单、能力标签、路径锚定、shell 分级、审批与 TOCTOU、沙箱、注入防御、钩子 |
| [features/prompt-context.md](features/prompt-context.md) | 提示词与上下文：稳定前缀与缓存、六段布局与预算、admission gate、压缩与卸载 |
| [features/memory.md](features/memory.md) | 分层记忆：working / episodic / durable、召回、冲突消解、遗忘、记忆投毒防御 |
| [features/sessions-and-runs.md](features/sessions-and-runs.md) | 会话与运行工件：session v2、run 目录、租约、动作账本、恢复/分叉/回退、索引与保留 |
| [features/evaluation.md](features/evaluation.md) | 评测：L0–L4 证据分层、任务挖掘、verifier 硬化、统计口径、judge 校准、对抗套件 |

## 速查参考

| 文档 | 覆盖 |
| --- | --- |
| [reference/configuration.md](reference/configuration.md) | 全部配置项、默认值、优先级、`.env` 与 `.moss/config.json` 的分工 |
| [reference/cli.md](reference/cli.md) | `moss` 与全部子命令、CLI 参数、REPL 斜杠命令、工具清单 |
| [reference/storage.md](reference/storage.md) | `.moss/` 下每个文件的用途、格式与生命周期 |

## 技术决策

只记录那些"换个做法整个项目就是另一个样子"的决定。

| 决策 | 一句话 |
| --- | --- |
| [decisions/0001-zero-dependencies.md](decisions/0001-zero-dependencies.md) | 零第三方运行时依赖，外部能力一律"探测到就用、探测不到就降级" |
| [decisions/0002-fail-closed-extensions.md](decisions/0002-fail-closed-extensions.md) | 外部能力（MCP / skill / code mode）一律 fail-closed，不静默降级 |
| [decisions/0003-evidence-layers.md](decisions/0003-evidence-layers.md) | 评测证据分 L0–L4，不同层的结论不许混写 |

## 计划

- [plans/active/](plans/active/) —— 尚未完成的设计与优化方案。
- [plans/archive/](plans/archive/) —— 已经落地的方案原稿，留作决策来源的考古材料。

## 原始设计稿（specs）

`docs/specs/` 是九份分模块设计稿，是当初实现的依据，字比 features 细得多（含验收标准与反例）。
**features 文档是当前事实，spec 是当初的设计**——两者冲突时以代码和 features 为准。

| spec | 对应功能文档 |
| --- | --- |
| [spec-01-repo-context.md](specs/spec-01-repo-context.md) | [repo-context](features/repo-context.md) |
| [spec-02-agent-loop.md](specs/spec-02-agent-loop.md) | [agent-loop](features/agent-loop.md) |
| [spec-03-tool-safety.md](specs/spec-03-tool-safety.md) | [tool-safety](features/tool-safety.md) |
| [spec-04-prompt-cache.md](specs/spec-04-prompt-cache.md) | [prompt-context](features/prompt-context.md) |
| [spec-05-memory.md](specs/spec-05-memory.md) | [memory](features/memory.md) |
| [spec-06-context.md](specs/spec-06-context.md) | [prompt-context](features/prompt-context.md) |
| [spec-07-session-artifacts.md](specs/spec-07-session-artifacts.md) | [sessions-and-runs](features/sessions-and-runs.md) |
| [spec-08-evaluation.md](specs/spec-08-evaluation.md) | [evaluation](features/evaluation.md) |
| [spec-09-new-modules.md](specs/spec-09-new-modules.md) | 分散在上面各篇的"扩展点"小节 |

---

## 写文档的约定

- 中文，解释**为什么存在 / 在链路里的位置**，而不是复述代码能读到的东西。
- 引用代码用相对路径 + 行号（`moss/agent_loop.py:131`），行号会漂，**结论不要依赖行号**。
- 精确的数值（默认值、阈值、路径）集中在 `reference/`，功能文档里只引用不复制，避免两处打架。
- 一个事实只写一处。CLAUDE.md 写"改代码时必须守住的不变量"，docs 写"这东西是怎么回事"。
