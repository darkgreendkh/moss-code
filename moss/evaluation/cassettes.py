"""L1 磁带的存放约定与查找（spec-09 §9.8）。

为什么单独一个模块：磁带目录的布局是评测和录制脚本之间的契约。
两边各拼一次路径，改一次目录结构就会出现"录到 A、回放读 B"的静默不命中。

布局（`benchmarks/cassettes/<prompt_version>/<task_id>/`）：prompt 版本进路径是
刻意的——prompt 一改，指纹全变，旧磁带本来就不该被新 prompt 复用。分目录之后
"旧磁带失效"表现为找不到目录、老实回落脚本，而不是一路 miss 到底。
"""

from __future__ import annotations

from pathlib import Path

from ..context.prefix import PROMPT_VERSION
from ..providers.recording import Cassette, ReplayModelClient

CASSETTE_SUBDIR = Path("benchmarks") / "cassettes"

# 磁带的来源。它必须落进 manifest 与评测工件：
# 从脚本引导出来的磁带**不能**声称是真实模型轨迹，那是 L0/L1 的证据边界。
SOURCE_SCRIPTED_BOOTSTRAP = "scripted-bootstrap"
SOURCE_PROVIDER = "provider"

# 这些任务的请求**做不到**跨 workspace 路径稳定，所以不录磁带，老实留在脚本上。
# 记在这里而不是让它们静默 miss：miss 会被当成"harness 变了"，
# 而这两条其实是录制方案本身的边界。
UNCASSETTABLE_TASKS = {
    # 预算被压到 900 token，prompt 在**规范化之前**就被按字符截断了。
    # 截断点取决于 workspace 绝对路径的长度，而临时目录每次都不一样长——
    # 指纹规范化能替掉路径，替不掉"路径长了两个字符所以少截一行"。
    "context_reduction_checkpoint": "prompt is budget-clipped, so absolute path length leaks into the text",
    # 这条任务的模型输出里**故意**含一个 secret 形状的字符串（验的就是记忆层
    # 会不会拒绝它）。磁带落盘前必须脱敏，脱完这条任务就没得验了。
    "durable_promotion_reject": "the expected model output is secret-shaped on purpose and redaction destroys it",
}


def cassette_root(repo_root, prompt_version=PROMPT_VERSION):
    return Path(repo_root) / CASSETTE_SUBDIR / str(prompt_version)


def cassette_dir(repo_root, task_id, prompt_version=PROMPT_VERSION):
    return cassette_root(repo_root, prompt_version) / str(task_id)


def has_cassette(repo_root, task_id, prompt_version=PROMPT_VERSION):
    directory = cassette_dir(repo_root, task_id, prompt_version)
    return bool(Cassette(directory).entry_paths())


def cassette_source(repo_root, task_id, prompt_version=PROMPT_VERSION):
    manifest = Cassette(cassette_dir(repo_root, task_id, prompt_version)).read_manifest()
    return str(manifest.get("source", "") or "")


def build_replay_client(repo_root, task_id, workspace, *, on_miss="fail", prompt_version=PROMPT_VERSION):
    """给一个任务装配回放 client。找不到磁带返回 None，调用方回落脚本。"""
    directory = cassette_dir(repo_root, task_id, prompt_version)
    if not Cassette(directory).entry_paths():
        return None
    return ReplayModelClient(
        directory,
        on_miss=on_miss,
        root=getattr(workspace, "repo_root", None),
        miss_observer=None,
    )


def cassette_task_ids(repo_root, prompt_version=PROMPT_VERSION):
    root = cassette_root(repo_root, prompt_version)
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if Cassette(item).entry_paths())
