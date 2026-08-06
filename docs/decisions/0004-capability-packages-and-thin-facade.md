# 0004 · 按能力分包，保留薄 facade

- **状态**：已采纳并实施
- **影响范围**：`moss/` 运行时代码、内部导入路径、测试与架构文档
- **相关**：[整体架构](../architecture.md) · [零第三方运行时依赖](0001-zero-dependencies.md)

## 背景

`moss/` 根目录已经有 46 个 Python 文件，根目录代码约 1.55 万行。
问题不只是文件多：`runtime.py` 1772 行，`Moss` 有 136 个方法并直接依赖
34 个内部模块；`context/manager.py`、`cli/`、`execution/registry.py` 也分别超过
900 行。新增能力已经开始在三种组织方式之间摇摆：

- `providers/`、`extensions/mcp/` 按技术能力分包；
- `features/` 只装了 memory，名字不能说明边界；
- 其余运行时能力仍然全部平铺在根目录。

现有 [architecture.md](../architecture.md#4-模块地图) 已经按主循环、上下文、
工具安全、持久化、观测和扩展点描述了真实职责，但源码布局没有表达这张地图。

## 决定

采用“**薄入口 + 按能力分包 + 组合式 facade**”的混合结构：

```text
moss/
├── __init__.py / __main__.py
├── cli/                    # 参数、装配、REPL、子命令
├── runtime.py              # Moss 公共兼容 facade
├── atomic_io.py / clock.py / config.py
├── agent/                  # 主循环、状态、预算、停滞、输出解析
├── context/                # prompt、历史、token、压缩、仓库上下文
│   └── repository/
├── execution/              # 工具协议、注册、执行与安全护栏
│   └── safety/
├── memory/                 # working / episodic / durable
├── runs/                   # run/session/checkpoint/lease/ledger/rewind
│   └── observability/
├── extensions/             # skill/hook/delegate/router/code mode/MCP
├── providers/
└── evaluation/             # 不在运行时主链路
```

这不是传统的 `controller/service/dao/model`，也不是强行引入端口适配器、
Repository interface 等企业模板。Moss 是控制循环，不是 CRUD 系统；目录边界以
“哪些文件因同一种原因一起变化”为准。

## 依赖规则

1. `moss/__init__.py` 继续导出 `Moss`、`SessionStore`、`WorkspaceContext` 和 provider clients；
   `from moss import Moss` 保持兼容。
2. `cli` 和 `runtime` 可以装配全部能力包；能力包不得反向导入 `moss.cli` 或 `moss.runtime`。
   `evaluation` 是测试/评测适配层，可以使用公共 runtime。
3. `atomic_io.py`、`clock.py`、`config.py` 保持小而稳定，只能依赖标准库。
4. 能力包之间允许少量显式单向依赖，例如 execution 使用 runs 的账本与
   context 的 workspace 失效通知；禁止形成新的循环导入。
5. `Moss` 通过组合 `ContextService`、`ExecutionService`、`RunCoordinator`、
   `ExtensionManager` 收缩职责；不使用 mixin 把共享状态藏进继承树。
6. 目录移动与职责拆分分开提交。单个阶段要么只移动并改 import，要么只拆实现，
   不在同一提交里顺手改变运行时行为。

## 公共与内部兼容边界

长期兼容的是 `moss/__init__.py` 声明的公共 API 和 `moss` CLI。
`moss.task_state`、`moss.tools` 等平铺路径属于内部 API；迁移阶段由测试和仓库内代码
一次性更新，不永久保留四十多个 shim。需要短期兼容的高扇入类型只在原路径保留
一个发布周期的 re-export，并在计划中逐项列出，避免 shim 变成第二套结构。

## 不做什么

- 不新增第三方运行时依赖。
- 不把每个小文件都变成目录，也不设机械的“500 行必须拆”规则。
- 不为了目录好看改变落盘格式、CLI 参数、trace 事件名、工具顺序或安全策略。
- 不同时重写评测框架；`evaluation/` 只跟随必要的 import 更新。

## 结果与代价

实施后，根目录只保留公共入口、组合 facade 和三个稳定基础模块；找代码可以先按能力定位。
代价是迁移期 import 变化较多，因此必须有包布局测试、公共 API 测试、循环导入检查，
并按小阶段运行全量测试。真正的收益来自后续把 `Moss` 收缩为 facade；只移动文件
而不拆职责只能算第一阶段，不算完成。
