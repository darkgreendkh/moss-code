"""shell 命令风险分级。

为什么重写：原来的分级是纯子串/前缀匹配，`ls; rm -rf /` 因为以 `ls` 开头
被判成 read_only，直接免审批放行。前缀匹配只看命令开头，而一条 shell 命令
可以有任意多段。

这里的做法是先把复合命令按 `; && || |` 换行拆成 argv 列表，再逐段看
`argv[0]` 的 basename，整条命令取所有段的最高风险。分不出来的（命令替换、
eval、引号不闭合）一律进 high 并标 undecidable——静态分析认输的时候，
正确的行为是交给人，不是猜一个低风险。

安全不能靠"一律拒绝"换：误报会把用户逼去用 `--approval auto`，那等于把
护栏整个关掉。所以常见只读命令必须老老实实判成 read_only。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# 风险等级，从低到高。整条命令取各段最大值。
RISK_ORDER = ("read_only", "test", "write", "network", "high", "denied")

# 这些前缀是 wrapper，真正的命令在它们后面。剥掉才能看到真正的 argv[0]。
_WRAPPERS = {"env", "nice", "nohup", "time", "command", "builtin", "exec", "stdbuf", "xargs"}
# sudo 也是 wrapper，但它本身就把风险抬到 high——提权是提权，和它跑什么无关。
_PRIVILEGE_WRAPPERS = {"sudo", "doas", "su"}

READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "file", "stat", "pwd", "echo", "printf",
    "rg", "grep", "egrep", "fgrep", "sed", "awk", "cut", "sort", "uniq", "tr",
    "diff", "tree", "which", "type", "basename", "dirname", "realpath", "date",
    "dir", "findstr", "get-content", "select-string",
}
TEST_COMMANDS = {"pytest", "tox", "nox", "ruff", "mypy", "pyright", "flake8", "eslint", "tsc", "jest", "vitest"}
WRITE_COMMANDS = {"mkdir", "mv", "cp", "touch", "ln", "chmod", "chown", "tee", "patch", "sed-i"}
NETWORK_COMMANDS = {"curl", "wget", "nc", "netcat", "ssh", "scp", "rsync", "ftp", "telnet", "http", "httpie"}
# 解释器：带 -c/-e 时无法静态判定要跑什么，一律 high。
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php", "bash", "sh", "zsh", "fish", "dash"}
# 包管理器：装东西就是下载 + 执行安装脚本，永远按 network 处理。
PACKAGE_MANAGERS = {"pip", "pip3", "npm", "pnpm", "yarn", "uv", "poetry", "gem", "cargo", "go", "brew", "apt", "apt-get"}

# git 子命令分级。git 是最常用的命令，分不细会让整类操作都要审批。
GIT_READ_ONLY = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe", "blame", "shortlog"}
GIT_WRITE = {"add", "commit", "restore", "switch", "checkout", "stash", "tag", "merge", "rebase", "cherry-pick"}
GIT_NETWORK = {"push", "fetch", "pull", "clone", "submodule"}

# 无法静态判定的信号。命中即 high + undecidable。
_SUBSTITUTION = re.compile(r"\$\(|`|\$\{[^}]*\(")
_UNDECIDABLE_COMMANDS = {"eval", "source", "."}

# fork bomb 的经典形状。它不是"某个命令"，是一段函数定义，只能按文本认。
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{.*\|.*&.*\}\s*;?\s*:")

# 摧毁性删除的目标。归一化后比对，覆盖 -rf / -fr / --recursive --force。
_CATASTROPHIC_PATHS = {"/", "/*", "~", "~/", "/home", "/usr", "/etc", "/var", "/bin", "/lib", "/system"}


@dataclass(frozen=True)
class ShellRisk:
    level: str
    reasons: tuple = ()
    argvs: tuple = ()
    undecidable: bool = False

    def __str__(self):
        # 旧调用点把返回值当字符串用（进 metadata、写 trace），保持可读。
        return self.level

    def __eq__(self, other):
        # 兼容老代码里的 `classify_shell_command(cmd) == "read_only"` 写法。
        if isinstance(other, str):
            return self.level == other
        if isinstance(other, ShellRisk):
            return (self.level, self.reasons, self.argvs, self.undecidable) == (
                other.level,
                other.reasons,
                other.argvs,
                other.undecidable,
            )
        return NotImplemented

    def __hash__(self):
        return hash(self.level)

    @property
    def denied(self):
        return self.level == "denied"


def _max_level(levels):
    best = "read_only"
    for level in levels:
        if RISK_ORDER.index(level) > RISK_ORDER.index(best):
            best = level
    return best


def _split_unquoted_lines(command):
    """按引号外的换行切分。引号里的换行属于内容，不是分隔符。"""
    lines = []
    current = []
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
            current.append(char)
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char == "\n":
            lines.append("".join(current))
            current = []
            continue
        current.append(char)
    lines.append("".join(current))
    return [line for line in lines if line.strip()] or [""]


def split_command_line(command):
    """把复合命令拆成 argv 列表。

    用 shlex 的 punctuation_chars 模式：它会把 `;` `&&` `||` `|` 当成独立 token，
    同时保留引号语义——`echo "a; b"` 里的分号不是分隔符。
    解析失败（引号不闭合）时抛 ValueError，由调用方判 undecidable。
    """
    command = str(command or "")
    # 换行也是命令分隔符，但 shlex 把它当普通空白，`ls\nrm -rf x` 会被并成一条。
    # 所以先按"引号外的换行"切开，再逐行 lex。
    tokens = []
    for line in _split_unquoted_lines(command):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens.extend(list(lexer))  # 引号不闭合会在这里抛 ValueError
        tokens.append(";")
    segments = []
    current = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&", "\n", ";;"} or set(token) <= {";", "&", "|"} and token:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_wrappers(argv):
    """剥掉 env/nice/time 这类前缀，返回 (真正的 argv, 是否提权)。"""
    privileged = False
    index = 0
    while index < len(argv):
        token = argv[index]
        name = token.rsplit("/", 1)[-1]
        if "=" in token and not token.startswith("-"):
            # `env A=1 cmd` 或直接 `A=1 cmd`
            index += 1
            continue
        if name in _PRIVILEGE_WRAPPERS:
            privileged = True
            index += 1
            continue
        if name in _WRAPPERS:
            index += 1
            continue
        break
    return argv[index:], privileged


def _is_catastrophic_delete(argv):
    flags = set()
    targets = []
    for token in argv[1:]:
        if token.startswith("--"):
            flags.add(token.lstrip("-").lower())
        elif token.startswith("-"):
            flags.update(token[1:].lower())
        else:
            targets.append(token)
    recursive = "r" in flags or "recursive" in flags
    forced = "f" in flags or "force" in flags
    if not (recursive or forced):
        return False
    for target in targets:
        normalized = target.rstrip("/") or "/"
        if normalized.lower() in _CATASTROPHIC_PATHS or target in _CATASTROPHIC_PATHS:
            return True
    return False


def _classify_git(argv):
    subcommand = next((token for token in argv[1:] if not token.startswith("-")), "")
    if subcommand in GIT_NETWORK:
        return "network", f"git {subcommand} talks to a remote"
    if subcommand in GIT_WRITE:
        return "write", f"git {subcommand} modifies the working tree"
    if subcommand in GIT_READ_ONLY:
        return "read_only", ""
    if subcommand == "config" and any(token == "--global" for token in argv):
        return "high", "git config --global changes settings outside the repo"
    if subcommand in {"reset", "clean"}:
        return "high", f"git {subcommand} can discard work irreversibly"
    return "high", f"unrecognized git subcommand: {subcommand or '(none)'}"


def _classify_find(argv):
    # find 本身只读，但 -delete / -exec 让它变成任意执行。
    if "-delete" in argv:
        return "high", "find -delete removes files"
    if "-exec" in argv or "-execdir" in argv or "-ok" in argv:
        return "high", "find -exec runs arbitrary commands"
    return "read_only", ""


def _classify_interpreter(argv, name):
    if any(flag in argv[1:] for flag in ("-c", "-e", "--eval", "--command")):
        return "high", f"{name} -c runs code that cannot be inspected statically"
    if "-m" in argv:
        # `python -m pytest` 是最常见的测试入口，按模块名定级。
        index = argv.index("-m")
        module = argv[index + 1] if index + 1 < len(argv) else ""
        if module in {"pytest", "unittest"}:
            return "test", ""
        return "write", f"{name} -m {module or '(none)'}"
    script = next((token for token in argv[1:] if not token.startswith("-")), "")
    if not script:
        return "high", f"{name} without a script starts an interactive shell"
    if script.startswith("/") or script.startswith(".."):
        return "high", f"{name} runs a script outside the workspace: {script}"
    return "write", f"{name} runs {script}"


def classify_argv(argv):
    """给一段 argv 定级。只看 argv[0] 的 basename（先剥 wrapper）。"""
    argv = [str(token) for token in argv if str(token)]
    if not argv:
        return ShellRisk(level="read_only", argvs=())
    stripped, privileged = _strip_wrappers(argv)
    if not stripped:
        return ShellRisk(level="high", reasons=("command is only wrappers",), argvs=(tuple(argv),))
    name = stripped[0].rsplit("/", 1)[-1].lower()
    reasons = []
    level = "high"

    if name in _UNDECIDABLE_COMMANDS:
        return ShellRisk(
            level="high",
            reasons=(f"{name} evaluates text as code",),
            argvs=(tuple(argv),),
            undecidable=True,
        )
    if name == "rm":
        if _is_catastrophic_delete(stripped):
            return ShellRisk(
                level="denied",
                reasons=("recursive delete of a root or home path",),
                argvs=(tuple(argv),),
            )
        level, reason = "high", "rm deletes files"
    elif name == "chmod" and any(token in {"777", "-R", "--recursive"} for token in stripped[1:]) and any(
        token.rstrip("/") in {"", "/", "~"} for token in stripped[1:] if not token.startswith("-")
    ):
        return ShellRisk(level="denied", reasons=("chmod on a root path",), argvs=(tuple(argv),))
    elif name == "git":
        level, reason = _classify_git(stripped)
    elif name == "find":
        level, reason = _classify_find(stripped)
    elif name in INTERPRETERS:
        level, reason = _classify_interpreter(stripped, name)
    elif name in PACKAGE_MANAGERS:
        level, reason = _classify_package_manager(stripped, name)
    elif name in NETWORK_COMMANDS:
        level, reason = "network", f"{name} performs network I/O"
    elif name in TEST_COMMANDS:
        level, reason = "test", ""
    elif name in READ_ONLY_COMMANDS:
        level, reason = _classify_read_only(stripped, name)
    elif name in WRITE_COMMANDS:
        level, reason = "write", f"{name} modifies files"
    elif name == "make":
        target = next((token for token in stripped[1:] if not token.startswith("-")), "")
        level, reason = ("test", "") if target in {"test", "lint", "check"} else ("write", f"make {target}")
    else:
        level, reason = "high", f"unknown executable: {name}"

    if reason:
        reasons.append(reason)
    if privileged:
        # 提权本身就是风险，和它跑什么无关。
        level = _max_level([level, "high"])
        reasons.append("runs with elevated privileges")
    return ShellRisk(level=level, reasons=tuple(reasons), argvs=(tuple(argv),))


def _classify_package_manager(argv, name):
    subcommand = next((token for token in argv[1:] if not token.startswith("-")), "")
    if name == "go" and subcommand == "test":
        return "test", ""
    if name == "cargo" and subcommand in {"test", "clippy", "fmt"}:
        return "test", ""
    if name in {"npm", "pnpm", "yarn"} and subcommand in {"test", "run"}:
        return "test", ""
    if name == "uv" and subcommand == "run":
        # `uv run pytest` 很常见：按它真正要跑的东西定级。
        inner = [token for token in argv[argv.index("run") + 1 :] if not token.startswith("-")]
        if inner:
            return classify_argv(inner).level, ""
        return "write", "uv run without a command"
    return "network", f"{name} {subcommand} downloads and executes packages"


def _classify_read_only(argv, name):
    # sed -i 是原地改写，不是只读。
    if name == "sed" and any(token.startswith("-i") for token in argv[1:]):
        return "write", "sed -i rewrites files in place"
    if name in {"chmod", "chown"}:
        return "write", f"{name} changes file metadata"
    return "read_only", ""


def _pipes_into_interpreter(segments, command):
    """`curl x | sh` 这类：下载的内容直接被执行，等于把控制权交给远端。"""
    if "|" not in command:
        return False
    names = []
    for argv in segments:
        stripped, _ = _strip_wrappers(argv)
        if stripped:
            names.append(stripped[0].rsplit("/", 1)[-1].lower())
    for index, name in enumerate(names[:-1]):
        if name in NETWORK_COMMANDS and names[index + 1] in INTERPRETERS:
            return True
    return False


def classify_shell_command(command):
    """给整条命令定级。旧名保留，返回类型升级成 ShellRisk。"""
    text = str(command or "").strip()
    if not text:
        return ShellRisk(level="high", reasons=("empty command",), undecidable=True)

    if _FORK_BOMB.search(text):
        return ShellRisk(level="denied", reasons=("fork bomb shape",), undecidable=True)

    undecidable = bool(_SUBSTITUTION.search(text))
    try:
        segments = split_command_line(text)
    except ValueError as exc:
        return ShellRisk(
            level="high",
            reasons=(f"cannot parse command: {exc}",),
            undecidable=True,
        )
    if not segments:
        return ShellRisk(level="high", reasons=("no executable found",), undecidable=True)

    if _pipes_into_interpreter(segments, text):
        return ShellRisk(
            level="denied",
            reasons=("downloaded content is piped straight into an interpreter",),
            argvs=tuple(tuple(argv) for argv in segments),
        )

    risks = [classify_argv(argv) for argv in segments]
    denied = next((risk for risk in risks if risk.denied), None)
    if denied is not None:
        return ShellRisk(
            level="denied",
            reasons=denied.reasons,
            argvs=tuple(tuple(argv) for argv in segments),
        )

    reasons = tuple(reason for risk in risks for reason in risk.reasons)
    level = _max_level([risk.level for risk in risks])
    if undecidable:
        # 静态分析认输时交给人，而不是猜一个低风险。
        level = _max_level([level, "high"])
        reasons = reasons + ("command substitution cannot be analysed statically",)
    return ShellRisk(
        level=level,
        reasons=reasons,
        argvs=tuple(tuple(argv) for argv in segments),
        undecidable=undecidable,
    )


def classify_shell_command_level(command):
    """过渡函数：只要等级字符串。"""
    return classify_shell_command(command).level
