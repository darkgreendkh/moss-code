"""Argument parser for the interactive and one-shot CLI."""

import argparse

from ..providers.recording import ON_MISS_CHOICES
from ..runs import checkpoint as checkpointlib
from ..execution.safety import sandbox as sandboxlib
from .factory import DEFAULT_OLLAMA_HOST, PROVIDER_CHOICES


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
):
    """既在每个选项后附上默认值，又保留 epilog 里手排的示例块换行。

    单用 ArgumentDefaultsHelpFormatter 会把 epilog 当普通段落重排，示例的
    每行命令被挤成一坨；混入 RawDescriptionHelpFormatter 就只对 description/
    epilog 保持原样，选项区仍照常补默认值。
    """


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=_HelpFormatter,
        description="Minimal coding agent for DeepSeek, OpenAI-compatible, Anthropic-compatible, or Ollama models.",
        epilog=(
            "Examples:\n"
            "  moss                                  start the interactive REPL\n"
            '  moss "排查测试失败并给出修复方案"       run once, print the answer, exit\n'
            "  moss --cwd /path/to/repo              point the agent at another workspace\n"
            "  moss --approval auto                  don't stop for approval on risky tools\n"
            "  moss runs list                        browse past run audit artifacts\n"
        ),
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")

    # --help 过去是 30+ 个开关平铺成一坨，新手无从下手。按职责分组只改帮助
    # 的排版、不改任何解析行为，让人能按"我想调哪方面"快速定位。
    connection = parser.add_argument_group(
        "connection", "which model backend to talk to and how"
    )
    connection.add_argument("--cwd", default=".", help="Workspace directory.")
    connection.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="Model backend to use. Defaults to MOSS_PROVIDER or deepseek.",
    )
    connection.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, MOSS_OPENAI_MODEL for openai, MOSS_ANTHROPIC_MODEL for anthropic, and MOSS_DEEPSEEK_MODEL for deepseek when set.",
    )
    connection.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    connection.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")
    connection.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    connection.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")

    session = parser.add_argument_group(
        "session", "resume, fork, or inspect a previous session"
    )
    session.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    session.add_argument(
        "--resume-parts",
        default="all",
        help=(
            "Comma-separated subset of the saved session to restore "
            f"({', '.join(checkpointlib.RESUME_PART_NAMES)}). Defaults to all."
        ),
    )
    session.add_argument(
        "--explain",
        action="store_true",
        help="Print what resuming would restore (freshness diff, identity mismatches, unconfirmed actions) and exit.",
    )
    session.add_argument(
        "--fork",
        default=None,
        metavar="CHECKPOINT_ID",
        help="Branch a new session from a checkpoint of the resumed session instead of continuing it.",
    )

    safety = parser.add_argument_group(
        "safety", "approvals, capability scoping, sandboxing, and secret redaction"
    )
    safety.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    safety.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    safety.add_argument(
        "--sandbox",
        choices=sandboxlib.SANDBOX_MODES,
        default="auto",
        help="Isolation layer for run_shell. auto uses whatever the platform supports; degradation is reported.",
    )
    safety.add_argument(
        "--allow-network",
        dest="allowed_network_hosts",
        default="",
        help="Comma-separated host allowlist for network commands. Empty means no host filtering.",
    )
    safety.add_argument(
        "--allow",
        dest="allow_rules",
        action="append",
        default=[],
        metavar="CAP[=GLOBS]",
        help="Restrict a capability to a comma-separated glob scope, e.g. --allow fs_write=src/**,tests/**.",
    )
    safety.add_argument(
        "--deny",
        dest="deny_rules",
        action="append",
        default=[],
        metavar="CAP[=GLOBS]",
        help="Deny a capability entirely (--deny network) or for some paths (--deny fs_write=.github/**).",
    )
    safety.add_argument(
        "--injection-scan",
        choices=("on", "off"),
        default="on",
        help="Scan tool output for prompt-injection attempts; a hit forces approval for the rest of the run.",
    )

    loop = parser.add_argument_group(
        "control loop", "how the agent iterates, verifies, and budgets a run"
    )
    loop.add_argument("--max-steps", type=int, default=25, help="Maximum tool/model iterations per request.")
    loop.add_argument(
        "--verify-before-final",
        choices=("on", "off"),
        default="on",
        help="Ask for one verification run before finalizing when files changed but nothing was tested.",
    )
    loop.add_argument(
        "--parallel-tools",
        choices=("on", "off"),
        default="off",
        help="Run a batch of read-only tool calls concurrently. Risky tools always run serially.",
    )
    loop.add_argument("--max-new-tokens", type=int, default=4096, help="Maximum model output tokens per step.")
    # 多维预算。默认全 None：不设就完全按老行为跑，只有 --max-steps 生效。
    loop.add_argument("--max-input-tokens", type=int, default=None, help="Stop the run after this many input tokens.")
    loop.add_argument("--max-output-tokens", type=int, default=None, help="Stop the run after this many output tokens.")
    loop.add_argument("--max-seconds", type=float, default=None, help="Stop the run after this much wall-clock time.")
    loop.add_argument("--max-usd", type=float, default=None, help="Stop the run after this much estimated spend.")
    loop.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    loop.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")

    prompt = parser.add_argument_group(
        "prompt & context", "prompt protocol, caching, and context compaction"
    )
    prompt.add_argument(
        "--no-prompt-cache",
        action="store_true",
        help="Disable provider prompt-cache fields for this process.",
    )
    prompt.add_argument(
        "--tool-protocol",
        choices=("auto", "native", "text"),
        default="auto",
        help="Choose provider-native tools automatically or force native/text protocol.",
    )
    prompt.add_argument(
        "--context-mode",
        choices=("rerender", "append_only"),
        default="rerender",
        help="Render compressed history each turn or keep stable append-only messages.",
    )
    prompt.add_argument(
        "--compaction",
        choices=("off", "rule", "model"),
        default="off",
        help=(
            "Compact older history into a structured handoff when context gets tight. "
            "off keeps today's truncate-only behaviour and is the ablation baseline."
        ),
    )
    prompt.add_argument(
        "--reflect",
        choices=("off", "rule", "model"),
        default="rule",
        help="Distill procedural memory at run completion; model uses an aux summarizer when available.",
    )

    extensions = parser.add_argument_group(
        "extensions", "opt-in capabilities; all default to off or behaviour-preserving"
    )
    # code mode：模型写一段受限 Python 批量编排工具调用。**沙箱是硬前置**，
    # 没有沙箱即使开了这个开关也不会暴露 run_orchestration。
    extensions.add_argument(
        "--enable-code-mode",
        action="store_true",
        help="Expose run_orchestration. Requires a working sandbox; off by default.",
    )
    # 多模型路由：compaction 摘要、失败分类、记忆提炼这些脏活不需要主力模型，
    # 但调用次数可能比主线还多。两个都不给 = 全部回落主模型，行为不变。
    extensions.add_argument(
        "--aux-model",
        default=None,
        help="Cheap model for aux tasks (compaction, reflection, judging). Falls back to the main model.",
    )
    extensions.add_argument(
        "--aux-provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="Provider for the aux model; defaults to the main provider.",
    )
    # 确定性录制回放。录一次真实轨迹，之后离线、零成本、逐字节可复现地重跑。
    extensions.add_argument(
        "--record",
        default=None,
        metavar="DIR",
        help="Record every model call into a cassette directory (redacted before it lands on disk).",
    )
    extensions.add_argument(
        "--replay",
        default=None,
        metavar="DIR",
        help="Replay model calls from a cassette directory instead of calling the provider.",
    )
    extensions.add_argument(
        "--replay-on-miss",
        choices=ON_MISS_CHOICES,
        default="fail",
        help="What to do when a replayed request is not on the cassette. fail is the CI default.",
    )
    return parser
