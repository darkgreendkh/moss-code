# 0001 · 零第三方运行时依赖

- **状态**：已采纳，长期有效
- **影响范围**：全项目
- **相关**：[0002](0002-fail-closed-extensions.md)

## 决定

`pyproject.toml` 的 `dependencies` 恒为空。运行时只用 Python 3.10+ 标准库。
开发依赖（pytest / ruff）走 `dependency-groups.dev`，不进运行时。

需要外部能力时一律**探测到就用、探测不到就降级**，并且降级要可见。

## 为什么

moss 的定位是"轻量好用的本地 coding agent"。对这个定位来说，
依赖不是中性的成本，它直接决定了这个工具能不能被用起来：

1. **安装即用。** 一个 coding agent 最常见的使用场景是"进到一个陌生仓库里干活"。
   如果它自己需要先解决一遍依赖冲突，价值就大打折扣。
2. **依赖是攻击面。** 这个工具会读你的代码、执行 shell、持有 API key。
   每多一个传递依赖，供应链风险就多一分，而这里的风险后果比一般库高得多。
3. **依赖会腐烂。** 项目里最能说明问题的一次事故来自开发依赖：
   ruff 0.16 扩大了默认 `select`，一次上游发版让 CI 冒出 100+ 条从没打算开启的告警。
   现在 `pyproject.toml` 里把规则集显式钉死成 `["E4", "E7", "E9", "F"]`。
   运行时依赖出这种事，代价只会更大。

## 代价与具体做法

这条约束逼出了一批手写实现。它们都比引库的版本弱，但**弱在可预期的地方**：

| 需求 | 常规做法 | moss 的做法 | 弱在哪 |
| --- | --- | --- | --- |
| HTTP | `requests` | stdlib `urllib` | 要自己处理重试、超时、流式 |
| `.env` | `python-dotenv` | 手写解析器 | 不支持 `$VAR` 展开，只读字面量 |
| frontmatter | `PyYAML` | 手写解析器 | 只支持扁平 key + 简单列表 |
| `.gitignore` 匹配 | `pathspec` | 手写匹配器（`context/repository/ignore.py`） | 边角语法可能不一致 |
| 符号索引 | tree-sitter | stdlib `ast`（Python）+ 行首前缀（其它） | 非 Python 语言准确率低 |
| token 计数 | `tiktoken` | 启发式估算 + 在线校准 | 有偏差，靠校准收敛 |
| 沙箱 | 各种 SDK | 直接调 `sandbox-exec` / `bwrap` / docker CLI | 平台相关 |
| MCP | 官方 SDK | 手写 JSON-RPC 2.0 over stdio | 只实现用得到的部分 |
| OTel | opentelemetry SDK | stdlib 生成 OTLP/JSON | 只落文件，不推 collector |

三条配套纪律：

### 1. 探测，不声明

`tiktoken` 装了就用真值，没装就用估算——**用 import 探测，不写进 dependencies**。
`rg` 存在就用它加速 `search_text`，不存在退回纯 Python 实现。
docker 能用就上 L3 沙箱，不能就退 L2。

### 2. 降级必须可见

这条比第 1 条重要。一个静默降级的沙箱比没有沙箱更危险——
用户以为自己跑在容器里。所以：

- 沙箱降级 → 进 `report.sandbox` **且**打 stderr。
- 目录 fsync 在 Windows 上不可用 → 进 `report.durability_degradations`。
- token 估算偏差 > 30% → 告警 `token_estimate_drift` 并退回 ratio 1.0。

### 3. 弱实现要知道自己弱在哪

手写匹配器的边角行为差异，写在模块注释里，而不是等它变成一个"诡异的 bug"。
`context/repository/ignore.py` 是最典型的例子——正因为它是手写的、可能不完全对，
**安全判定（路径锚定）绝不依赖它**，它只用来少扫少展示。

## 什么情况下会重新考虑

- 某个能力用 stdlib 实现会显著不安全（而不只是麻烦）。
- 某个手写实现的维护成本超过了它省下的依赖成本，且有稳定、无传递依赖的替代品。

到目前为止没有出现这两种情况。
