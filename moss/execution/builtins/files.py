"""Workspace and artifact file tools."""

import os
import shutil
import subprocess

from ... import atomic_io
from ...context.repository.workspace import IGNORED_PATH_NAMES

NOFOLLOW_SUPPORTED = hasattr(os, "O_NOFOLLOW")


def write_text_atomic(path, content):
    # 不允许写穿软链：审批之后目标被换成指向仓库外的软链，写入就落到了别处。
    # 原子替换本身会替掉软链而不是跟随它，但先显式拒绝能让这次尝试留下痕迹。
    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path.name}")
    # 原子 + fsync 统一走 atomic_io：agent 写用户源码是最不该"看起来写了其实没落盘"
    # 的一类写入。
    atomic_io.write_atomic(path, content)


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    # start 落在文件末尾之后：宁可报错也不要返回空字符串。
    # 静默的空结果模型没法解释，它只会换个区间再来一次。
    if start > len(lines):
        raise ValueError(f"start is past the end of the file ({len(lines)} lines)")
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_read_artifact(context, args):
    """按行区间取回一份被卸载的工具输出。

    存在的意义是让"截断"从有损变成可逆：prompt 里只放摘要 + 指针，
    模型真的需要那 1800 行时还能自己拿回来，而不是永远丢了。
    """
    path = context.run_path(args["path"])
    if not path.is_file():
        raise ValueError("path is not an artifact file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if start > len(lines):
        raise ValueError(f"start is past the end of the artifact ({len(lines)} lines)")
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.name} (lines {start}-{min(end, len(lines))} of {len(lines)})\n{body}"


def tool_search_text(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=context.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(context.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, content)
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_edit_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    write_text_atomic(path, text.replace(old_text, str(args["new_text"]), 1))
    return f"edited {path.relative_to(context.root)}"

