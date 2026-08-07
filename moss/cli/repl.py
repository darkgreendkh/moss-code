"""Terminal rendering and input helpers for the interactive CLI."""

import locale
import shutil
import textwrap

from ..context.token_budget import middle

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
    /help      Show this help message.
    /config    Show the current runtime settings (model, approval, verify, ...).
    /approval  Show or change the approval policy: /approval auto|ask|never.
    /verify    Show or toggle the pre-final verification check: /verify on|off.
    /model     Show or switch the model for this session: /model <name>.
    /approvals Show remembered approve/deny decisions; /approvals clear resets them.
    /memory    Show the agent's distilled working memory.
    /session   Show the path to the saved session directory.
    /reload    Reload tools and skills from disk.
    /rewind    Undo the last N file-changing steps (files + history + memory).
               /rewind 2 undoes two steps; /rewind! forces past your own edits.
    /reset     Clear the current session history and memory.
    /exit      Exit the agent.

    Examples:
      moss "排查测试失败并给出修复方案"     one-shot: run once, print the answer, exit.
      moss --approval auto                  don't stop for approval on risky tools.
      moss --cwd /path/to/repo              point the agent at another workspace.

    During a task: each step (thinking / tool / result) streams to stderr;
    the final answer goes to stdout. Press Ctrl-C to cancel and return to the prompt.
    """
).strip()

# 会话内可调的运行时开关。为什么存在：30+ 个开关过去只能在启动时定死，
# 想临时收紧/放松审批、换个模型，就只能退出重启、丢掉整段会话上下文。
# 这几个字段都是主循环每轮实时读取的，所以就地改就能立刻生效。


PROVIDER_KEY_ENV = {
    "deepseek": "MOSS_DEEPSEEK_API_KEY",
    "openai": "MOSS_OPENAI_API_KEY",
    "anthropic": "MOSS_ANTHROPIC_API_KEY",
}


def render_missing_key_hint(provider):
    """没配 key 时给一段能照着做的引导，而不是一句干巴巴的 warning。

    首次上手最常见的踩坑：装完直接跑，拿到一坨 HTTP 报错还不知道要设什么。
    这里把"下一步该做什么"摆出来——填哪个变量、填到哪、以及"手上没有 key
    可以直接跑本地模型"这条退路。
    """
    env = PROVIDER_KEY_ENV.get(provider, "the provider API key")
    return "\n".join(
        [
            f"! no API key found for provider '{provider}' — requests will fail until one is set.",
            "  next steps:",
            "    1. cp .env.example .env",
            f"    2. add to .env:  {env}=<your key>",
            "    3. re-run moss",
            "  no key handy? run a local model instead:  moss --provider ollama",
        ]
    )


def apply_approval(agent, argument):
    """/approval [auto|ask|never]：查看或切换审批策略。"""
    argument = (argument or "").strip().lower()
    if not argument:
        return f"approval policy: {agent.approval_policy}"
    if argument not in ("ask", "auto", "never"):
        return "usage: /approval ask|auto|never"
    agent.approval_policy = argument
    return f"approval policy set to {argument}"


def apply_verify(agent, argument):
    """/verify [on|off]：查看或切换收尾前的验证自检。"""
    argument = (argument or "").strip().lower()
    if not argument:
        return f"verify-before-final: {'on' if agent.verify_before_final else 'off'}"
    if argument not in ("on", "off"):
        return "usage: /verify on|off"
    agent.verify_before_final = argument == "on"
    return f"verify-before-final set to {argument}"


def apply_model(agent, argument):
    """/model [name]：查看或切换本会话使用的模型。

    只改 model_client.model 这个字符串——client 在每次请求时实时读它，
    所以不必重建连接。认不出模型能力的 client 没有该属性时如实拒绝。
    """
    argument = (argument or "").strip()
    client = getattr(agent, "model_client", None)
    current = getattr(client, "model", None)
    if current is None:
        return "this backend does not expose a switchable model"
    if not argument:
        return f"model: {current}"
    client.model = argument
    # 能力档随模型而变（缓存/native/窗口）；能重算就重算，重算不了就保持原样。
    recompute = getattr(client, "recompute_capabilities", None)
    if callable(recompute):
        try:
            recompute()
        except Exception:
            pass
    return f"model set to {argument} (was {current})"


def render_approvals(agent, argument=""):
    """/approvals [clear]：查看或清空本会话记住的"总是允许/拒绝"决定。

    为什么需要：过去按了 a=always / d=never 之后，这条决定只存在内存里，
    用户既看不见也撤不回——批错了只能重启整个会话。这里把它变成可见、可清。
    """
    argument = (argument or "").strip().lower()
    if argument == "clear":
        cleared = agent.clear_approval_memory()
        return f"cleared {cleared} remembered approval decision(s)"
    if argument:
        return "usage: /approvals [clear]"
    remembered = agent.remembered_approvals()
    if not remembered:
        return "no remembered approval decisions this session"
    lines = ["remembered approval decisions (this session):"]
    for (name, risk, bucket), allowed in sorted(remembered.items()):
        verdict = "always allow" if allowed else "always deny "
        scope = f" · {bucket}" if bucket else ""
        lines.append(f"  {verdict}  {name} [{risk}]{scope}")
    lines.append("  (/approvals clear resets these)")
    return "\n".join(lines)


def render_config(agent):
    """/config：把当前生效的运行时设置列出来，省得靠记忆猜。"""
    client = getattr(agent, "model_client", None)
    workspace = getattr(agent, "workspace", None)
    rows = [
        ("model", getattr(client, "model", "?")),
        ("provider", getattr(client, "provider", "?")),
        ("approval", agent.approval_policy),
        ("verify", "on" if getattr(agent, "verify_before_final", True) else "off"),
        ("max_steps", getattr(agent, "max_steps", "?")),
        ("workspace", getattr(workspace, "cwd", "?")),
        ("branch", getattr(workspace, "branch", "?")),
        ("session", agent.session.get("id", "?") if getattr(agent, "session", None) else "?"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label.ljust(width)}  {value}" for label, value in rows)


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


def _format_token_count(value):
    # token 数动辄上千，原样打出来一屏都是数字。压成 1.2k / 3.4M 这种量级读得更快。
    value = int(value or 0)
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.1f}M".replace(".0M", "M")


def render_run_summary(summary):
    """把一次 ask() 的收尾统计渲染成 stderr 上的一小块反馈。

    为什么存在：会动用户代码的 agent 跑完，用户第一件想知道的就是"它改了
    哪几个文件、验证了没有、花了多少"。这些数据一直躺在 report.json 里，但
    交互用户从来看不到，只能自己 `git status` 反查——掌控感的最大缺口。

    走 stderr（不是 stdout）：最终答案才是这次调用的产出，要能被管道接走；
    这块摘要是元信息，和进度/警告同属一路。返回空串表示不值得打（没有可报的）。
    """
    if not summary:
        return ""
    parts = []
    changed = list(summary.get("changed_files") or [])
    if changed:
        noun = "file" if len(changed) == 1 else "files"
        parts.append(f"{len(changed)} {noun} changed")
    steps = int(summary.get("tool_steps", 0) or 0)
    parts.append(f"{steps} step" + ("" if steps == 1 else "s"))
    in_tok = int(summary.get("input_tokens", 0) or 0)
    out_tok = int(summary.get("output_tokens", 0) or 0)
    if in_tok or out_tok:
        prefix = "~" if summary.get("usage_estimated") else ""
        parts.append(f"{prefix}{_format_token_count(in_tok)} in / {_format_token_count(out_tok)} out tok")
    usd = summary.get("usd")
    if usd is not None:
        parts.append(f"${float(usd):.3f}")
    if changed:
        # 验证只对"改了文件"的运行有意义：纯问答没有可验证的东西。
        parts.append("verified ✓" if summary.get("verified") else "unverified")
    status = str(summary.get("status", ""))
    stop_reason = str(summary.get("stop_reason", ""))
    if status == "failed":
        parts.append(f"failed ({stop_reason or 'unknown'})")
    elif status == "stopped" and stop_reason and stop_reason != "final_answer_returned":
        # 非正常收尾（撞步数/预算上限等）也要如实标一句：答案可能是强制收尾产出的，
        # 用户得知道它是"完整回答"还是"被截断后的尽力而为"。
        label = stop_reason.replace("_reached", "").replace("_", " ")
        parts.append(f"stopped: {label}")
    head = "  · " + " · ".join(parts)
    if not changed:
        return head
    # 改动文件单列一行、按空格排开，最多列 8 个，多的折叠成计数。
    shown = changed[:8]
    tail = "    " + "  ".join(shown)
    if len(changed) > len(shown):
        tail += f"  (+{len(changed) - len(shown)} more)"
    return head + "\n" + tail


def format_tool_result(payload):
    """把一次工具执行的结果压成一行反馈。返回空串表示不值得单独打一行。

    为什么存在：过去只有失败才吭声，成功的 read/shell/search 结果一律沉默——
    用户看着 `> run_shell pytest`、`> read_file foo.py` 却不知道跑出了什么、
    读到了什么，整个过程是黑盒。这里给成功的工具也补一行"它看见了什么"。
    """
    parts = []
    exit_code = payload.get("exit_code")
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    diff = payload.get("diff_summary") or []
    if diff:
        created = sum(1 for item in diff if str(item).startswith("created:"))
        modified = sum(1 for item in diff if str(item).startswith("modified:"))
        deleted = sum(1 for item in diff if str(item).startswith("deleted:"))
        chunks = []
        if created:
            chunks.append(f"+{created} new")
        if modified:
            chunks.append(f"~{modified} changed")
        if deleted:
            chunks.append(f"-{deleted} deleted")
        if chunks:
            parts.append(" ".join(chunks))
    # 没有结构化信号（读/搜/列目录）时，退回结果头一行——通常正好是那句
    # `# path (lines x-y of N)` 或第一条匹配，恰是用户最想瞄一眼的东西。
    preview = str(payload.get("preview", "")).strip()
    if not parts and preview:
        parts.append(middle(preview, 72))
    status = str(payload.get("status", "ok"))
    if status != "ok":
        parts.append(f"({status})")
    if not parts:
        # 纯无副作用、无输出的工具（update_plan 等）不强行占一行。
        return ""
    # 结果行标上工具名：一个 batch 是"先列全部调用、再列全部结果"（并发执行的必然
    # 顺序），结果和它的调用并不相邻。不标名时 `→ # CLAUDE.md (lines…)` 恰好紧贴在
    # `> list_files docs` 下面，会被读成"list_files 打印了行号范围"——其实那是上面
    # read_file 的结果。标上工具名就能一眼把每条结果归回它的调用。
    name = str(payload.get("name", ""))
    body = "  ".join(parts)
    return f"      → {name}  {body}" if name else f"      → {body}"


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
            line = format_tool_result(payload)
            if line:
                clear()
                stream.write(line + "\n")
                stream.flush()
        elif event == "error":
            # 收敛成"失败但已收尾"的运行时，stdout 上只有一句最终答案；
            # 具体原因（模型后端报错、prompt 装不下）要在 stderr 上说清楚。
            clear()
            scope = str(payload.get("scope", "run"))
            stream.write(f"  ! {scope}: {payload.get('message', '')}\n")
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
