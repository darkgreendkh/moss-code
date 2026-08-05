"""手写的 .gitignore 匹配器（零依赖）。

为什么存在：工作区快照、repo map、目录列举三处都要回答同一个问题——
“这个路径该不该被看见”。三处各写一套的话，模型看到的目录树和快照统计的
文件集会对不上，diff 里就会冒出模型从没见过的路径。

支持的子集：`#` 注释、`!` 取反、目录尾斜杠、`*` / `**` / `?`、前导 `/` 锚定、
字符类 `[a-z]` / `[!abc]`。写坏的规则（比如没闭合的 `[`）整行忽略并在 stderr 警告一次。

spec-01 原本把字符类列为不支持项，但 Python 官方 .gitignore 模板里就有
`*.py[cod]`——本仓库自己也在用。不支持意味着每次启动都要往 stderr 打一条警告，
而翻成正则字符类只是几行代码，所以这里做了支持。

安全边界：这个匹配器只用来“少扫一些文件”和“少展示一些文件”。
路径逃逸这类安全判定永远走 `Moss.path()` 的 resolve + 锚定检查，不依赖这里。
"""

import fnmatch
import re
import sys
from pathlib import Path

# 无论 .gitignore 怎么写，这些目录都不该进快照/地图：要么是工具自己的状态目录，
# 要么是体量大到会淹没一切的构建产物。
DEFAULT_IGNORE_PATTERNS = (
    ".git/",
    ".moss/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".venv/",
    "venv/",
    "node_modules/",
    ".DS_Store",
)

_WARNED_PATTERNS = set()


def _warn_once(pattern, reason):
    if pattern in _WARNED_PATTERNS:
        return
    _WARNED_PATTERNS.add(pattern)
    print(f"warning: ignoring unsupported gitignore pattern {pattern!r}: {reason}", file=sys.stderr)


def _char_class_regex(segment, start):
    """把 gitignore 的 [abc] / [a-z] / [!abc] 翻成正则字符类。

    返回 (regex, 下一个待扫描的下标)；没有闭合的 `]` 就返回 (None, start)，
    由调用方按“不支持的写法”处理。
    """
    index = start + 1
    negated = index < len(segment) and segment[index] in "!^"
    if negated:
        index += 1
    body = []
    # gitignore 沿用 shell 语义：紧跟在 [ 或 [! 后面的 ] 是字面量。
    if index < len(segment) and segment[index] == "]":
        body.append("\\]")
        index += 1
    while index < len(segment) and segment[index] != "]":
        char = segment[index]
        if char == "\\" and index + 1 < len(segment):
            body.append(re.escape(segment[index + 1]))
            index += 2
            continue
        body.append(char if char == "-" else re.escape(char))
        index += 1
    if index >= len(segment) or not body:
        return None, start
    prefix = "^" if negated else ""
    return f"[{prefix}{''.join(body)}]", index + 1


def _segment_regex(segment):
    """把一个路径段翻成正则；不支持的写法返回 None。"""
    out = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            translated, next_index = _char_class_regex(segment, index)
            if translated is None:
                return None
            out.append(translated)
            index = next_index
            continue
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


class _Rule:
    __slots__ = ("regex", "negated", "dir_only", "source")

    def __init__(self, regex, negated, dir_only, source):
        self.regex = regex
        self.negated = negated
        self.dir_only = dir_only
        self.source = source


def _compile(pattern):
    """把一行 gitignore 规则编译成 _Rule；不支持的写法返回 None。"""
    raw = pattern
    pattern = pattern.rstrip()
    if not pattern or pattern.startswith("#"):
        return None
    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if not pattern:
        return None
    # gitignore 语义：规则里出现斜杠（尾斜杠不算）就锚定到 .gitignore 所在目录，
    # 否则匹配任意深度的同名条目。
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    if not pattern:
        return None

    segments = pattern.split("/")
    body = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            body.append(".*" if last else "(?:[^/]+/)*")
            continue
        translated = _segment_regex(segment)
        if translated is None:
            _warn_once(raw, "malformed character class")
            return None
        body.append(translated)
        if not last:
            body.append("/")
    prefix = "" if anchored else "(?:.*/)?"
    return _Rule(re.compile(f"^{prefix}{''.join(body)}$"), negated, dir_only, raw)


class IgnoreRules:
    """按 .gitignore 语义决定路径是否被忽略。

    规则按“后写覆盖先写”生效（gitignore 的 last-match-wins）。
    """

    def __init__(self, rules=()):
        self._rules = tuple(rules)

    @classmethod
    def from_patterns(cls, patterns):
        rules = []
        for pattern in patterns:
            rule = _compile(pattern)
            if rule is not None:
                rules.append(rule)
        return cls(rules)

    @classmethod
    def load(cls, root, *, extra_patterns=(), use_defaults=True):
        patterns = list(DEFAULT_IGNORE_PATTERNS) if use_defaults else []
        gitignore = Path(root) / ".gitignore"
        if gitignore.exists():
            try:
                patterns.extend(gitignore.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
        patterns.extend(extra_patterns)
        return cls.from_patterns(patterns)

    def _match_self(self, rel_path, is_dir):
        ignored = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if not rule.regex.match(rel_path):
                continue
            ignored = not rule.negated
        return ignored

    def match(self, rel_path, *, is_dir=False):
        """rel_path 是相对仓库根的 POSIX 路径。

        先判祖先目录：gitignore 里被排除的目录，其内容无法用 `!` 重新纳入，
        所以任一祖先被忽略就直接返回 True，省掉后面的匹配。
        """
        rel_path = str(rel_path).replace("\\", "/").strip("/")
        if not rel_path:
            return False
        parts = rel_path.split("/")
        for depth in range(1, len(parts)):
            if self._match_self("/".join(parts[:depth]), True):
                return True
        return self._match_self(rel_path, is_dir)

    def __bool__(self):
        return bool(self._rules)


def parse_exclude_globs(value):
    """解析 `MOSS_SNAPSHOT_EXCLUDE` 这类逗号分隔的 glob 列表。"""
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


def matches_any_glob(rel_path, globs):
    """给 glob 列表做一次 fnmatch，用于不需要完整 gitignore 语义的场合。"""
    rel_path = str(rel_path).replace("\\", "/")
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in globs)
