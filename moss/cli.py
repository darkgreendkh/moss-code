"""命令行入口。

这个模块负责把“用户怎么启动 moss”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import locale
import os
from pathlib import Path
import shutil
import sys
import textwrap

from .config import load_project_env, provider_env
from .features.memory import reject_memory_reason
from .features.memory_records import SourceRef, make_record
from .features.memory_store import MemoryStore, project_scope_key
from . import injection as injectionlib
from .providers.clients import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import Moss, SessionStore
from .token_budget import middle
from . import policy as policylib
from . import sandbox as sandboxlib
from .workspace import WorkspaceContext

DEFAULT_SECRET_ENV_NAMES = (
    "MOSS_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "MOSS_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "MOSS_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

# 欢迎屏的左侧图形：一棵五层的树，轮廓用斜线、树冠用点、每层外侧缀星。
# 全部字符在等宽字体里占一格，所以整屏的盒子宽度可以直接按 len() 算。
WELCOME_ART = (
    "      ✦      ",
    "     ✧╱╲✧    ",
    "    ✦╱∴∴╲✦   ",
    "   ✧╱∴∴∴∴╲✧  ",
    "  ✦╱∴∴∴∴∴∴╲✦ ",
    " ✧╱∴∴∴∴∴∴∴∴╲✧",
)
WELCOME_NAME = "moss"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "small loop, real tools"
WELCOME_HINT = "/help for commands   ·   ctrl-c cancels the current task"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session file.
    /reload  Reload tools and skills from disk.
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.

    Press Ctrl-C during a task to cancel it and return to the prompt.
    """
).strip()


DEFAULT_OLLAMA_MODEL = "qwen3:8b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5-5"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_PROVIDER = "deepseek"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")
SECRET_ENV_NAMES_VAR = "MOSS_SECRET_ENV_NAMES"


def _effective_provider(args):
    # Provider 选择优先级：
    # 1. 用户显式传入 --provider
    # 2. 项目 .env / shell 里的 MOSS_PROVIDER
    # 3. 代码里的默认 provider
    provider = getattr(args, "provider", None) or provider_env(
        "MOSS_PROVIDER", default=DEFAULT_PROVIDER
    )
    if provider not in PROVIDER_CHOICES:
        choices = ", ".join(PROVIDER_CHOICES)
        raise ValueError(f"unknown provider: {provider}. expected one of: {choices}")
    return provider


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("MOSS_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("MOSS_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("MOSS_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = _effective_provider(args)
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MOSS_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        # 只回落到本 provider 自己的 key：base URL 指向官方 endpoint 后，
        # 拿别家的 key 去请求必定 401，跨 provider 回落只会把错误藏成"认证失败"。
        api_key = provider_env("MOSS_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            provider="openai",
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MOSS_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env("MOSS_ANTHROPIC_API_KEY", ("ANTHROPIC_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            provider="anthropic",
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MOSS_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("MOSS_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            provider="deepseek",
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def _ctype_codeset():
    # 直接问 C 层 locale 的真实 codeset。不用 locale.getpreferredencoding()：
    # Python 处于 UTF-8 mode 时它无条件返回 utf-8，看不出 LC_CTYPE 其实还停在
    # ASCII——而 readline 的多字节行为只认后者。
    if not hasattr(locale, "nl_langinfo"):
        return ""
    try:
        return (locale.nl_langinfo(locale.CODESET) or "").lower().replace("-", "")
    except (ValueError, AttributeError):
        return ""


def enable_line_editing():
    """给 REPL 的 input() 挂上真正的行编辑器，否则中文输入会把整个进程搞崩。

    为什么存在：
    macOS / BSD 的 tty 行规程没有 Linux 那个 IUTF8 标志，退格只删掉一个
    **字节**。一个汉字是 3 字节，删一次就在输入缓冲里留下半个 UTF-8 序列，
    input() 用严格 UTF-8 解码时抛 UnicodeDecodeError，REPL 直接带着 traceback
    退出（英文一字符一字节，所以只有中文会踩到）。同时终端只擦掉 1 列，而汉字
    占 2 列，回显也跟着错位。把 readline 挂上之后，行编辑由 readline/libedit
    接管：退格按“字符”删、方向键也不再变成 ^[[D 这样的乱码。

    在 agent 链路里的位置：
    main() 进入 REPL 之前调用一次，纯副作用。任何一步失败都必须静默降级——
    Windows 标准库没有 readline，不能因为它启动不了 moss（REPL 里还有一层
    解码兜底）。
    """
    # readline/libedit 是否按字符处理多字节取决于 LC_CTYPE：locale 还在 C/ASCII
    # 时它照样把汉字拆成字节，所以必须先把 locale 拉到 UTF-8 再 import。
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if _ctype_codeset() != "utf8":
        # LANG 没设或被设成 C 的环境（CI、cron、精简 Docker 镜像）走这里。
        for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
            try:
                locale.setlocale(locale.LC_ALL, candidate)
                break
            except locale.Error:
                continue
    try:
        import readline  # noqa: F401  仅为副作用导入：让 input() 走行编辑器
    except ImportError:
        pass


def _scrub_undecodable(text):
    """把输入里的孤立代理字符替换掉。

    Python 处于 UTF-8 mode 时 stdin 用 surrogateescape 解码，半个 UTF-8 序列
    不会抛错，而是变成孤立代理字符一路带下去，直到写 session JSON 或发 HTTP
    请求时才炸（或者更糟：静默把乱码发给模型）。在入口就地清掉。
    """
    if not any("\ud800" <= ch <= "\udfff" for ch in text):
        return text
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def _format_tool_line(name, args):
    args = args or {}
    if name in ("read_file", "list_files"):
        detail = str(args.get("path", "."))
    elif name == "search_text":
        detail = str(args.get("pattern", ""))
    elif name in ("write_file", "edit_file"):
        detail = str(args.get("path", ""))
    elif name == "run_shell":
        detail = str(args.get("command", ""))
    elif name == "delegate":
        detail = str(args.get("task", ""))
    elif name == "use_skill":
        detail = str(args.get("name", ""))
    else:
        detail = ""
    detail = middle(detail, 72)
    return f"  > {name}  {detail}".rstrip()


def make_progress_printer(stream):
    """把 agent 的进度事件渲染成终端里逐行滚动的活动反馈。

    交互式 coding agent 最劝退的一点，就是敲完请求后要盯着空屏幕等一整个
    工具循环跑完。这个渲染器让每一步（在想什么、调了哪个工具、结果如何）
    都实时可见。它只写 stderr，所以 stdout 里仍然只有最终答案，方便管道使用。
    """
    state = {"pending": False}

    def clear():
        if state["pending"]:
            try:
                stream.write("\r" + " " * 72 + "\r")
                stream.flush()
            except Exception:
                pass
            state["pending"] = False

    def render(event, payload):
        if event == "thinking":
            clear()
            step = payload.get("step")
            max_steps = payload.get("max_steps")
            stream.write(f"\r  ... thinking ({step}/{max_steps})")
            stream.flush()
            state["pending"] = True
        elif event == "tool":
            clear()
            stream.write(_format_tool_line(payload.get("name", ""), payload.get("args")) + "\n")
            stream.flush()
        elif event == "tool_result":
            status = str(payload.get("status", "ok"))
            if status != "ok":
                stream.write(f"      ({status})\n")
                stream.flush()

    render.clear = clear
    return render


def build_welcome(agent, model, host):
    """渲染 REPL 启动时的欢迎屏。

    为什么存在：
    这是用户看到的第一屏，要在一眼之内回答四个问题——我连的是哪个模型、
    在哪个工作区/分支、审批策略松还是紧、这轮会话叫什么。这些都是事后
    翻 trace 时最常需要对齐的字段，所以放在最显眼的位置。

    布局约定：整屏是一个等宽盒子，每行渲染完长度必须完全相同（有测试守着），
    所以所有可变文本都先过 `middle()` 截断到固定宽度再 ljust 补齐。
    """
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    pad = 2
    inner = width - 2 - pad * 2
    art_width = max(len(art_line) for art_line in WELCOME_ART)
    art_gap = 3
    text_width = inner - art_width - art_gap
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, inner)
        return f"│{' ' * pad}{body.ljust(inner)}{' ' * pad}│"

    def top():
        return "╭" + "─" * (width - 2) + "╮"

    def divider():
        return "├" + "─" * (width - 2) + "┤"

    def bottom():
        return "╰" + "─" * (width - 2) + "╯"

    def banner(art_line, text):
        # 图形固定占左侧一列，文字靠左对齐排在右边——比整体居中更像现代 CLI，
        # 也让 name/subtitle/status 三行形成一条竖直的阅读线。
        body = middle(text, text_width)
        return row(f"{art_line.ljust(art_width)}{' ' * art_gap}{body}")

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"│{' ' * pad}{left}{' ' * gap}{right}{' ' * pad}│"

    # 三行文字底部对齐到图形最后三行——树冠越往下越宽，文字贴着宽的一侧才不虚，
    # 顶上的尖角顺势留出一块斜向留白。改图形的行数时这里会自动跟着走。
    lines = (WELCOME_NAME, WELCOME_SUBTITLE, WELCOME_STATUS)
    banner_text = ("",) * (len(WELCOME_ART) - len(lines)) + lines
    rows = [banner(art, text) for art, text in zip(WELCOME_ART, banner_text)]
    rows.extend(
        [
            row(""),
            divider(),
            row(""),
            row(cell("workspace", agent.workspace.cwd, inner)),
            pair("model", model, "branch", agent.workspace.branch),
            pair("approval", agent.approval_policy, "session", agent.session["id"]),
            row(""),
            divider(),
            row(WELCOME_HINT),
        ]
    )
    return "\n".join([top(), row(""), *rows, bottom()])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Moss 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Moss`，或一个从旧 session 恢复出来的 `Moss`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    policy = policylib.Policy.build(
        allow=policylib.parse_capability_rules(getattr(args, "allow_rules", [])),
        deny=policylib.parse_capability_rules(getattr(args, "deny_rules", [])),
    )
    allowed_network_hosts = tuple(
        host.strip() for host in str(getattr(args, "allowed_network_hosts", "") or "").split(",") if host.strip()
    )
    run_budget_limits = {
        "max_input_tokens": getattr(args, "max_input_tokens", None),
        "max_output_tokens": getattr(args, "max_output_tokens", None),
        "max_wall_clock_s": getattr(args, "max_seconds", None),
        "max_usd": getattr(args, "max_usd", None),
    }
    feature_flags = {
        "prompt_cache": not bool(getattr(args, "no_prompt_cache", False)),
    }
    tool_protocol = getattr(args, "tool_protocol", "auto")
    context_mode = getattr(args, "context_mode", "rerender")
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.moss/sessions")
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Moss.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            parallel_tools=getattr(args, "parallel_tools", "off") == "on",
            run_budget_limits=run_budget_limits,
            verify_before_final=getattr(args, "verify_before_final", "on") == "on",
            injection_scan=getattr(args, "injection_scan", "on") == "on",
            policy=policy,
            sandbox=getattr(args, "sandbox", "auto"),
            allowed_network_hosts=allowed_network_hosts,
            feature_flags=feature_flags,
            tool_protocol=tool_protocol,
            context_mode=context_mode,
            reflect_mode=getattr(args, "reflect", "rule"),
        )
    return Moss(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        parallel_tools=getattr(args, "parallel_tools", "off") == "on",
        run_budget_limits=run_budget_limits,
        verify_before_final=getattr(args, "verify_before_final", "on") == "on",
        injection_scan=getattr(args, "injection_scan", "on") == "on",
        policy=policy,
        sandbox=getattr(args, "sandbox", "auto"),
        allowed_network_hosts=allowed_network_hosts,
        feature_flags=feature_flags,
        tool_protocol=tool_protocol,
        context_mode=context_mode,
        reflect_mode=getattr(args, "reflect", "rule"),
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for DeepSeek, OpenAI-compatible, Anthropic-compatible, or Ollama models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="Model backend to use. Defaults to MOSS_PROVIDER or deepseek.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, MOSS_OPENAI_MODEL for openai, MOSS_ANTHROPIC_MODEL for anthropic, and MOSS_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=25, help="Maximum tool/model iterations per request.")
    parser.add_argument(
        "--sandbox",
        choices=sandboxlib.SANDBOX_MODES,
        default="auto",
        help="Isolation layer for run_shell. auto uses whatever the platform supports; degradation is reported.",
    )
    parser.add_argument(
        "--allow-network",
        dest="allowed_network_hosts",
        default="",
        help="Comma-separated host allowlist for network commands. Empty means no host filtering.",
    )
    parser.add_argument(
        "--allow",
        dest="allow_rules",
        action="append",
        default=[],
        metavar="CAP[=GLOBS]",
        help="Restrict a capability to a comma-separated glob scope, e.g. --allow fs_write=src/**,tests/**.",
    )
    parser.add_argument(
        "--deny",
        dest="deny_rules",
        action="append",
        default=[],
        metavar="CAP[=GLOBS]",
        help="Deny a capability entirely (--deny network) or for some paths (--deny fs_write=.github/**).",
    )
    parser.add_argument(
        "--injection-scan",
        choices=("on", "off"),
        default="on",
        help="Scan tool output for prompt-injection attempts; a hit forces approval for the rest of the run.",
    )
    parser.add_argument(
        "--verify-before-final",
        choices=("on", "off"),
        default="on",
        help="Ask for one verification run before finalizing when files changed but nothing was tested.",
    )
    parser.add_argument(
        "--parallel-tools",
        choices=("on", "off"),
        default="off",
        help="Run a batch of read-only tool calls concurrently. Risky tools always run serially.",
    )
    parser.add_argument(
        "--no-prompt-cache",
        action="store_true",
        help="Disable provider prompt-cache fields for this process.",
    )
    parser.add_argument(
        "--tool-protocol",
        choices=("auto", "native", "text"),
        default="auto",
        help="Choose provider-native tools automatically or force native/text protocol.",
    )
    parser.add_argument(
        "--context-mode",
        choices=("rerender", "append_only"),
        default="rerender",
        help="Render compressed history each turn or keep stable append-only messages.",
    )
    parser.add_argument(
        "--reflect",
        choices=("off", "rule", "model"),
        default="rule",
        help="Distill procedural memory at run completion; model uses an aux summarizer when available.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=4096, help="Maximum model output tokens per step.")
    # 多维预算。默认全 None：不设就完全按老行为跑，只有 --max-steps 生效。
    parser.add_argument("--max-input-tokens", type=int, default=None, help="Stop the run after this many input tokens.")
    parser.add_argument("--max-output-tokens", type=int, default=None, help="Stop the run after this many output tokens.")
    parser.add_argument("--max-seconds", type=float, default=None, help="Stop the run after this much wall-clock time.")
    parser.add_argument("--max-usd", type=float, default=None, help="Stop the run after this much estimated spend.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def build_memory_arg_parser():
    parser = argparse.ArgumentParser(prog="moss memory", description="Inspect and manage durable memory.")
    commands = parser.add_subparsers(dest="memory_command", required=True)

    def add_location(command, *, include_scope=True):
        command.add_argument("--cwd", default=".", help="Workspace for project memory.")
        if include_scope:
            command.add_argument("--scope", choices=("project", "global"), default="project")

    list_parser = commands.add_parser("list", help="List active memories.")
    add_location(list_parser)

    show_parser = commands.add_parser("show", help="Show one memory record.")
    show_parser.add_argument("id")
    add_location(show_parser)

    add_parser = commands.add_parser("add", help="Add an explicit user memory.")
    add_parser.add_argument("text")
    add_parser.add_argument("--topic", required=True)
    add_parser.add_argument("--tag", action="append", default=[])
    add_location(add_parser)

    forget_parser = commands.add_parser("forget", help="Append a tombstone for one memory.")
    forget_parser.add_argument("id")
    add_location(forget_parser)

    export_parser = commands.add_parser("export", help="Export active memories as JSON.")
    add_location(export_parser)
    return parser


def _memory_cli_store(args):
    if args.scope == "global":
        return MemoryStore(Path.home() / ".moss" / "memory"), "global"
    workspace = Path(args.cwd).resolve()
    return MemoryStore(workspace / ".moss" / "memory", workspace_root=workspace), project_scope_key(workspace)


def _memory_cli_source(record):
    if not record.source_refs:
        return "unknown"
    return ",".join(source.path or source.run_id or "unknown" for source in record.source_refs)


def _memory_cli_line(record):
    return (
        f"{record.id} [{record.scope} trust={record.trust} source={_memory_cli_source(record)}] "
        f"{record.topic}: {record.text}"
    )


def run_memory_command(argv):
    args = build_memory_arg_parser().parse_args(argv)
    store, scope_key = _memory_cli_store(args)
    command = args.memory_command

    if command == "list":
        records = store.active_records()
        print("\n".join(_memory_cli_line(record) for record in records) or "(no memories)")
        return 0
    if command == "show":
        record = store.get(args.id)
        if record is None:
            print(f"memory not found: {args.id}", file=sys.stderr)
            return 1
        print(_memory_cli_line(record))
        return 0
    if command == "export":
        print(json.dumps([record.to_dict() for record in store.active_records()], ensure_ascii=False, sort_keys=True))
        return 0
    if command == "forget":
        try:
            record = store.delete(args.id)
        except KeyError:
            print(f"memory not found: {args.id}", file=sys.stderr)
            return 1
        print(json.dumps({"deleted": record.id}, sort_keys=True))
        return 0

    text = str(args.text).strip()
    finding = injectionlib.scan(text, source="memory_cli")
    reason = "injection_suspected" if finding is not None else reject_memory_reason(text)
    if reason:
        reason = "too_noisy" if reason == "noisy_output" else reason
        print(json.dumps({"status": "rejected", "reason": reason}, sort_keys=True))
        return 1
    record = make_record(
        scope=args.scope,
        scope_key=scope_key,
        topic=args.topic,
        subject=" ".join(text.lower().split())[:120],
        text=text,
        tags=args.tag,
        trust="user",
        source_refs=(SourceRef(run_id="cli"),),
    )
    store.append(record)
    print(
        json.dumps(
            {"status": "written", "id": record.id, "scope": record.scope, "trust": record.trust},
            sort_keys=True,
        )
    )
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["memory"]:
        return run_memory_command(argv[1:])
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    # 装上实时进度渲染器，让工具循环里的每一步都对用户可见。
    progress = make_progress_printer(sys.stderr)
    agent.progress_observer = progress

    # 首次运行最常见的踩坑：没配 key 就直接调用，拿到一坨 HTTP 报错。
    # 这里提前给一句人话提示，指出该设哪个环境变量。Ollama 不需要 key，跳过。
    if hasattr(agent.model_client, "api_key") and not agent.model_client.api_key:
        provider = _effective_provider(args)
        env_hint = {
            "deepseek": "MOSS_DEEPSEEK_API_KEY (or DEEPSEEK_API_KEY)",
            "openai": "MOSS_OPENAI_API_KEY (or OPENAI_API_KEY)",
            "anthropic": "MOSS_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)",
        }.get(provider, "the provider API key")
        print(
            f"warning: no API key found for provider '{provider}'. "
            f"Set {env_hint} in your .env or environment before sending a request.",
            file=sys.stderr,
        )

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                answer = agent.ask(prompt)
            except KeyboardInterrupt:
                progress.clear()
                print("\n(cancelled)", file=sys.stderr)
                return 130
            except RuntimeError as exc:
                progress.clear()
                print(str(exc), file=sys.stderr)
                return 1
            progress.clear()
            print(answer)
            # 后端错误现在会被 agent 收敛成一次已收尾的失败运行（而不是抛异常），
            # 但 one-shot / CI 场景需要用退出码反映失败，否则脚本会误以为成功。
            task_state = getattr(agent, "current_task_state", None)
            if task_state is not None and task_state.status == "failed":
                return 1
        return 0

    enable_line_editing()

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        # 空行单独 print 而不塞进 prompt：readline 要靠 prompt 算显示宽度，
        # prompt 里带 \n 会让它的列计数错位，宽字符重绘就跟着花屏。
        print()
        try:
            user_input = _scrub_undecodable(input("moss> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0
        except UnicodeDecodeError:
            # 没有 readline 兜底的环境（Windows、被裁剪的构建）里，半个 UTF-8
            # 序列还是可能漏进来。丢掉这一行让用户重输，绝不因为一次误删
            # 就把整个会话带走。
            print(
                "warning: could not decode that input (broken multi-byte sequence); please retype it.",
                file=sys.stderr,
            )
            continue

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory.render_memory_details())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reload":
            result = agent.reload_registry()
            changed = result["added"] + result["removed"] + result["tools_added"] + result["tools_removed"]
            print("tools and skills reloaded" + (f" ({', '.join(changed)})" if changed else ""))
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            answer = agent.ask(user_input)
        except KeyboardInterrupt:
            # Ctrl-C 只取消当前这一轮任务，回到提示符，而不是退出整个程序。
            progress.clear()
            print("\n(cancelled)", file=sys.stderr)
            continue
        except RuntimeError as exc:
            progress.clear()
            print(str(exc), file=sys.stderr)
            continue
        progress.clear()
        print(answer)
