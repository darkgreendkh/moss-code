# 已落地的方案存档

这里的文档**已经完成**，留着是为了回答"当初为什么这么改"。
它们描述的是**改造前的状态和当时的判断**，不是当前事实——
当前事实看 [../../features/](../../features/) 与代码。

| 文档 | 覆盖 | 落地情况 |
| --- | --- | --- |
| [2026-agent-upgrade-plan.md](2026-agent-upgrade-plan.md) | 总方案：9 个章节 + 路线图 + 20 条 bug 清单 | 第 1–9 章全部落地为 spec-01 到 spec-09；第 11 章 20 条 bug 全部修复 |
| [2026-08-05-prompt-cache.md](2026-08-05-prompt-cache.md) | spec-04 的分阶段实施计划 | 完成 |
| [2026-08-05-structured-memory.md](2026-08-05-structured-memory.md) | spec-05 的分阶段实施计划 | 完成 |
| [2026-08-06-evaluation-framework.md](2026-08-06-evaluation-framework.md) | spec-08 的分阶段实施计划 | 框架完成；真实模型证据仍缺，见 [../active/l2-l3-evidence.md](../active/l2-l3-evidence.md) |
| [2026-08-06-capability-package-refactor.md](2026-08-06-capability-package-refactor.md) | 运行时代码按能力分包，并把 `Moss` 收缩为组合 facade | 完成 |

## 读总方案时注意

`2026-agent-upgrade-plan.md` 里的行号、文件规模、模块划分都是
**2026-08-05 的快照**，现在早已不同（当时 `agent_loop.py` 311 行，现在 865 行）。
它有价值的部分是**判断和理由**，不是那些具体的坐标。

其中第 0.2 节"这次大改必须守住的东西"仍然完全有效，
现在的对应位置是 `CLAUDE.md` 的"关键约定与不变量"和
[../../architecture.md](../../architecture.md#5-全局不变量)。
