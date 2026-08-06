"""按工具输出类型注册的压缩器（spec-06 §4.3）。

为什么存在：head/middle/tail 三种切片对所有输出一视同仁，而不同输出的
"信息在哪"完全不同——pytest 的关键信息是失败用例名和 assert 行（散落在中间），
ruff 是同一个 CODE 重复几百遍（可以聚合），git diff 是 hunk（切一半就读不懂了）。
按类型压缩才能在同样的预算里保住"失败原因"这类高价值信号。

只有**已经落盘成 artifact** 的输出才走这里：压缩是有损的，而有损只有在
"完整版还能用 read_artifact 取回"的前提下才可接受。

压缩器契约：`fn(text, budget) -> (compressed_text, stats)`，budget 单位是字符。
不可丢失清单（exit_code / 状态 / affected_paths / artifact 指针）由
`compress()` 与 `tool_executor` 共同保证，压缩器自己不需要操心。
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict

from .token_budget import clip_to_budget

# 每个失败用例保留的 traceback 末尾行数。取 15：够看到抛错点和它的调用者，
# 又不会把整条栈拖进来。
TRACEBACK_TAIL_LINES = 15
# lint 类输出每个 CODE 保留的样例条数。
LINT_SAMPLES_PER_CODE = 3
# 搜索结果每个文件保留的命中条数。
SEARCH_HITS_PER_FILE = 3
# 压缩后至少要留给正文的字符数，防止预算被 header 吃光后压出一个空字符串。
MIN_BODY_BUDGET = 200

_EXIT_CODE_PATTERN = re.compile(r"^exit_code:\s*-?\d+\s*$")
_PYTEST_CASE_HEADER = re.compile(r"^_{3,}\s*(?P<name>.+?)\s*_{3,}$")
_PYTEST_SUMMARY = re.compile(
    r"(^=+.*\b(passed|failed|error|errors|skipped|xfailed)\b.*=+$)|(^(FAILED|ERROR)\s+\S+)"
)
_PYTEST_MARKERS = ("= FAILURES =", "short test summary info", "=== FAILURES ===")
_LINT_LINE = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s+(?P<code>[A-Z]{1,4}\d{1,4}|error|warning|note)\b"
)
_SEARCH_LINE = re.compile(r"^(?P<path>[^\s:]+):(?P<line>\d+):(?P<body>.*)$")
_DIFF_FILE = re.compile(r"^diff --git a/(?P<path>\S+) b/\S+")
_HUNK_HEADER = re.compile(r"^@@ ")
_LIST_ENTRY = re.compile(r"^\[(?P<kind>[DF])\]\s+(?P<path>.+)$")
# "这一行在说成败"的关键词。压缩后一条都不剩就是 error_signal_lost。
_ERROR_SIGNAL = re.compile(
    r"(?i)(error|failed|failure|traceback|exception|fatal|assert|denied|not found|no such|cannot)"
)


def _split_exit_code(text):
    """run_shell 的输出以 `exit_code: N` 开头。它属于不可丢失清单，先摘出来。"""
    lines = str(text).splitlines()
    if lines and _EXIT_CODE_PATTERN.match(lines[0].strip()):
        return lines[0], "\n".join(lines[1:])
    return "", str(text)


def _has_error_signal(text):
    return bool(_ERROR_SIGNAL.search(str(text)))


def detect_kind(tool_name, args, text):
    """按工具名 + 输出形状判定压缩器类型。

    形状优先于工具名：`run_shell` 可能在跑 pytest、ruff 或 git diff，
    只看工具名等于把三类完全不同的输出都塞进同一个兜底切片。
    """
    tool_name = str(tool_name or "")
    text = str(text or "")
    if tool_name == "search_text":
        return "search_text"
    if tool_name == "list_files":
        return "list_files"

    _, body = _split_exit_code(text)
    head = body.lstrip()
    if head.startswith("stdout:"):
        head = head[len("stdout:"):].lstrip()
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return "generic"

    if any(marker in body for marker in _PYTEST_MARKERS):
        return "pytest"
    if _PYTEST_SUMMARY.search(body.strip().splitlines()[-1].strip() if body.strip() else ""):
        return "pytest"
    if head.startswith("diff --git") or any(_DIFF_FILE.match(line) for line in lines[:20]):
        return "git_diff"
    lint_hits = sum(1 for line in lines if _LINT_LINE.match(line.strip()))
    if lint_hits >= 3 and lint_hits >= len(lines) // 2:
        return "lint"
    return "generic"


def compress_pytest(text, budget):
    """保住失败用例名、assert 行和 traceback 末尾，其余整块丢掉。

    pytest 输出里 90% 是通过的用例和重复的环境信息；决策只依赖失败的那几个。
    """
    lines = str(text).splitlines()
    blocks = _pytest_failure_blocks(lines)
    kept = []
    failed_cases = []
    for name, block in blocks:
        failed_cases.append(name)
        kept.append(f"___ {name} ___")
        indexes = {
            index
            for index, line in enumerate(block)
            if line.strip().startswith(("E ", "assert ", "E\t"))
        }
        indexes.update(range(max(0, len(block) - TRACEBACK_TAIL_LINES), len(block)))
        kept.extend(block[index] for index in sorted(indexes))
    summary_lines = [line for line in lines if _PYTEST_SUMMARY.search(line.strip())]
    # 汇总行放最后：它回答"一共几个失败"，是模型判断有没有改完的唯一依据。
    for line in summary_lines:
        if line not in kept:
            kept.append(line)
    if not kept:
        return _clip(text, budget, keep="tail"), {"failed_cases": [], "kept_lines": 0}
    return _clip("\n".join(kept), budget, keep="head"), {
        "failed_cases": failed_cases,
        "kept_lines": len(kept),
        "total_lines": len(lines),
    }


def _pytest_failure_blocks(lines):
    blocks = []
    current_name = ""
    current = []
    for line in lines:
        match = _PYTEST_CASE_HEADER.match(line.strip())
        if match:
            if current_name:
                blocks.append((current_name, current))
            current_name = match.group("name")
            current = []
            continue
        if current_name:
            if _PYTEST_SUMMARY.search(line.strip()) and line.strip().startswith("="):
                blocks.append((current_name, current))
                current_name = ""
                current = []
                continue
            current.append(line)
    if current_name:
        blocks.append((current_name, current))
    return blocks


def compress_lint(text, budget):
    """按 CODE 聚合折叠：每类保留前几条 + 计数。

    ruff/mypy 的输出常常是同一个 CODE 重复几百遍。逐条读没有意义，
    "E501 一共 214 处，样例三条"才是模型需要的形状。
    """
    lines = str(text).splitlines()
    grouped = OrderedDict()
    others = []
    for line in lines:
        match = _LINT_LINE.match(line.strip())
        if not match:
            if line.strip():
                others.append(line)
            continue
        grouped.setdefault(match.group("code"), []).append(line)
    if not grouped:
        return _clip(text, budget, keep="head"), {"codes": {}}
    kept = []
    counts = {}
    for code, items in grouped.items():
        counts[code] = len(items)
        kept.append(f"{code}: {len(items)} occurrence(s)")
        kept.extend(items[:LINT_SAMPLES_PER_CODE])
        if len(items) > LINT_SAMPLES_PER_CODE:
            kept.append(f"  ... and {len(items) - LINT_SAMPLES_PER_CODE} more {code}")
    # 非 lint 形状的行（汇总行、报错）留在末尾：它们通常是"总共 N 个问题"。
    kept.extend(others[-5:])
    return _clip("\n".join(kept), budget, keep="head"), {
        "codes": counts,
        "total_findings": sum(counts.values()),
    }


def compress_search(text, budget):
    """每个文件最多留几条命中，再给一份文件级计数。

    搜索结果的价值是"在哪些文件里、大概多少处"，逐条列出第 40 个命中
    对下一步决策没有帮助。
    """
    lines = str(text).splitlines()
    per_file = OrderedDict()
    others = []
    for line in lines:
        match = _SEARCH_LINE.match(line.strip())
        if match:
            per_file.setdefault(match.group("path"), []).append(line)
        elif line.strip():
            others.append(line)
    if not per_file:
        return _clip(text, budget, keep="head"), {"files": {}}
    kept = []
    counts = {}
    for path, items in per_file.items():
        counts[path] = len(items)
        kept.extend(items[:SEARCH_HITS_PER_FILE])
        if len(items) > SEARCH_HITS_PER_FILE:
            kept.append(f"  ... {len(items) - SEARCH_HITS_PER_FILE} more match(es) in {path}")
    kept.append(f"matches: {sum(counts.values())} across {len(counts)} file(s)")
    kept.extend(others[:3])
    return _clip("\n".join(kept), budget, keep="head"), {
        "files": counts,
        "total_matches": sum(counts.values()),
    }


def compress_git_diff(text, budget):
    """按 hunk 保留；超预算就退回 hunk 头 + 增删计数。

    diff 从中间切一刀会得到一段读不懂的补丁：既不知道改的是哪个文件，
    也不知道上下文。宁可只给骨架。
    """
    lines = str(text).splitlines()
    files = _diff_files(lines)
    full = "\n".join(lines)
    stats = {
        "files": {path: {"added": added, "removed": removed} for path, _, added, removed in files},
    }
    if len(full) <= budget:
        return full, stats
    kept = []
    for path, hunks, added, removed in files:
        kept.append(f"diff --git a/{path} b/{path} (+{added}/-{removed})")
        kept.extend(hunks)
    return _clip("\n".join(kept), budget, keep="head"), stats


def _diff_files(lines):
    files = []
    path = ""
    hunks = []
    added = 0
    removed = 0
    for line in lines:
        match = _DIFF_FILE.match(line)
        if match:
            if path:
                files.append((path, hunks, added, removed))
            path = match.group("path")
            hunks = []
            added = 0
            removed = 0
            continue
        if not path:
            continue
        if _HUNK_HEADER.match(line):
            hunks.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if path:
        files.append((path, hunks, added, removed))
    return files


def compress_list_files(text, budget):
    """按扩展名聚合计数 + 保留目录清单。

    目录是导航需要的，几百个同类文件不是；"237 个 .py"比列出前 200 个更有用。
    """
    lines = str(text).splitlines()
    directories = []
    extensions = Counter()
    files = 0
    others = []
    for line in lines:
        match = _LIST_ENTRY.match(line.strip())
        if not match:
            if line.strip():
                others.append(line)
            continue
        if match.group("kind") == "D":
            directories.append(line)
            continue
        files += 1
        name = match.group("path").rsplit("/", 1)[-1]
        extensions["." + name.rsplit(".", 1)[1] if "." in name else "(no extension)"] += 1
    if not directories and not files:
        return _clip(text, budget, keep="head"), {"files": 0, "extensions": {}}
    kept = list(directories)
    kept.append(f"{files} file(s): " + ", ".join(f"{count}x {ext}" for ext, count in extensions.most_common()))
    kept.extend(others[:3])
    return _clip("\n".join(kept), budget, keep="head"), {
        "files": files,
        "directories": len(directories),
        "extensions": dict(extensions),
    }


def compress_generic(text, budget):
    """兜底：还是 head/middle/tail 切片。识别不出类型时不假装懂它的结构。"""
    return _clip(text, budget, keep="head"), {}


def _clip(text, budget, keep="head"):
    return clip_to_budget(str(text), max(MIN_BODY_BUDGET, int(budget)), measure=len, keep=keep)


_REGISTRY = {
    "pytest": compress_pytest,
    "lint": compress_lint,
    "search_text": compress_search,
    "git_diff": compress_git_diff,
    "list_files": compress_list_files,
    "generic": compress_generic,
}


def register(kind, fn):
    """注册（或覆盖）一类压缩器。项目可以按自己的工具链补充类型。"""
    _REGISTRY[str(kind)] = fn
    return fn


def registered_kinds():
    return tuple(_REGISTRY)


def compress(kind, text, budget):
    """按类型压缩，并保证不可丢失清单里属于文本的那部分活下来。

    `exit_code` 行在这里被摘出来单独置顶：它是判断成败的第一依据，
    绝不能因为正好落在被切掉的那一段而消失。
    """
    text = str(text)
    fn = _REGISTRY.get(str(kind), compress_generic)
    exit_line, body = _split_exit_code(text)
    reserve = len(exit_line) + 1 if exit_line else 0
    body_budget = max(MIN_BODY_BUDGET, int(budget) - reserve)
    compressed, stats = fn(body, body_budget)
    compressed = clip_to_budget(compressed, body_budget, measure=len, keep="head")
    result = f"{exit_line}\n{compressed}" if exit_line else compressed
    metadata = {
        "compressor_kind": str(kind),
        "compressed_chars": len(result),
        "original_chars": len(text),
        # 原文里有"说明成败"的行、压缩后一条都不剩 —— 这正是压缩最不该犯的错，
        # 所以把它做成一个可统计的标签而不是靠人工抽检。
        "error_signal_lost": bool(_has_error_signal(body) and not _has_error_signal(result)),
    }
    metadata.update({key: value for key, value in dict(stats or {}).items()})
    return result, metadata
