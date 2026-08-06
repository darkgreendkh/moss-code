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
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session directory.
    /reload  Reload tools and skills from disk.
    /rewind  Undo the last N file-changing steps (files + history + memory).
             /rewind 2 undoes two steps; /rewind! forces past your own edits.
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.

    Press Ctrl-C during a task to cancel it and return to the prompt.
    """
).strip()


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
