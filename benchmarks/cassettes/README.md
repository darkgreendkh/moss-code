# L1 回放磁带

一盘磁带 = 一个 benchmark 任务的全部模型调用，按**规范化后的请求指纹**索引
（见 [`moss/providers/recording.py`](../../moss/providers/recording.py)）。

```
<prompt_version>/<task_id>/
  manifest.json     录制时间、provider、model、prompt_version、source
  000-<fp12>.json   {"fingerprint", "request_digest", "response", "usage"}
```

## 目录里的 `source` 字段决定这盘带子能证明什么

| source | 含义 | 能支撑的结论 |
| --- | --- | --- |
| `scripted-bootstrap` | 模型输出来自 `SCRIPTED_MODEL_OUTPUTS`，只有**请求指纹**是真的 | 只能证明 harness 合同：同样的模型输出，新 harness 的执行结果有没有变 |
| `provider` | 接真实后端录的一次真实轨迹 | 仍然只是 L1。回放不能证明模型能力，能力结论只能来自 L2 |

## 重录

```bash
python3 scripts/record_cassettes.py --all                 # 从脚本引导
python3 scripts/record_cassettes.py --all --source provider  # 真实后端，要 key、要花钱
```

prompt 一改，指纹全变——所以磁带按 `prompt_version` 分目录，旧带子表现为
"找不到目录、老实回落脚本"，而不是一路 miss 到底。

## 两个任务没有磁带

`moss/evaluation/cassettes.py::UNCASSETTABLE_TASKS` 里登记了原因：一个的 prompt
被预算截断（截断点随绝对路径长度变化），一个的模型输出**故意**是 secret 形状
（磁带落盘必须脱敏，脱完就没得验了）。它们留在手写脚本上，这是有意的，不是遗漏。

## 落盘前一律脱敏

磁带进 git。`RecordingModelClient` 写盘前过 `redact_artifact`，
`tests/test_recording.py::test_committed_cassettes_carry_no_secret_shapes`
会把仓库里所有磁带再扫一遍。
