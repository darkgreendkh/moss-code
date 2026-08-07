"""Workspace and artifact file tools."""

import os
import shutil
import subprocess

from ... import atomic_io
from ...context.repository.workspace import IGNORED_PATH_NAMES
from ...context.token_budget import MAX_TOOL_OUTPUT
from ..specs import apply_defaults

NOFOLLOW_SUPPORTED = hasattr(os, "O_NOFOLLOW")

# 区间读渲染完的字符预算。留在卸载阈值（MAX_TOOL_OUTPUT）之下一截，给头部和续读
# 提示留余量：输出一旦越过阈值就被通用层卸载成 artifact，模型得再花一步 read_artifact
# 才能看到自己刚读的内容。密排中文尤其致命——300 行约 3 万字符，是阈值的两倍，
# 一次默认读必然两步。所以模型没显式指定 end（用的是默认区间）时按这个预算自适应收窄。
_READ_FIT_BUDGET = MAX_TOOL_OUTPUT - 512


def _render_line_range(tool_name, display_name, lines, start, end, *, fit):
    """把 [start, end] 行区间渲成带行号的正文 + 头部。

    头部必须报出总行数 `(lines x-y of N)`：只给路径的话，模型读完一段既不知道文件
    还有多长、也不知道自己读到哪了，只能靠猜下一个区间再来一次——这是同一个文件被
    反复读的直接来源。

    `fit=True`（模型没显式给 end，用的是默认区间）时按字符预算逐行累加，装不下就停，
    并在头部如实报出真实区间 + 续读位置；`fit=False`（模型显式要了一段大区间）保持
    原样渲染，超阈值时交给通用层卸载成 artifact，那是模型自己要的整块。
    """
    total = len(lines)
    end = min(end, total)
    if not fit:
        body = "\n".join(f"{n:>4}: {line}" for n, line in enumerate(lines[start - 1:end], start=start))
        return f"# {display_name} (lines {start}-{end} of {total})\n{body}"
    rendered = []
    size = 0
    last = start
    for number in range(start, end + 1):
        piece = f"{number:>4}: {lines[number - 1]}"
        # 至少渲一行，避免"单行超预算"时返回空——空结果模型没法解释。
        if rendered and size + len(piece) + 1 > _READ_FIT_BUDGET:
            break
        rendered.append(piece)
        size += len(piece) + 1
        last = number
    head = f"# {display_name} (lines {start}-{last} of {total})"
    if last < total:
        head += f" — more available; continue with {tool_name}(start={last + 1})"
    body = "\n".join(rendered)
    return f"{head}\n{body}"


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
    # 模型是否显式给了 end 决定要不要自适应收窄：没给（用默认区间）才收，避免密排
    # 中文文档一次默认读就撞破卸载阈值、白白多走一步 read_artifact；显式要的大区间
    # 照旧渲染，超阈值时交给通用层卸载。所以在 apply_defaults 补默认值之前先记下来。
    end_explicit = "end" in (args or {})
    args = apply_defaults("read_file", args)
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args["start"])
    end = int(args["end"])
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    # start 落在文件末尾之后：宁可报错也不要返回空字符串。
    # 静默的空结果模型没法解释，它只会换个区间再来一次。
    if start > len(lines):
        raise ValueError(f"start is past the end of the file ({len(lines)} lines)")
    return _render_line_range(
        "read_file", str(path.relative_to(context.root)), lines, start, end, fit=not end_explicit
    )


def tool_read_artifact(context, args):
    """按行区间取回一份被卸载的工具输出。

    存在的意义是让"截断"从有损变成可逆：prompt 里只放摘要 + 指针，
    模型真的需要那 1800 行时还能自己拿回来，而不是永远丢了。
    """
    end_explicit = "end" in (args or {})
    args = apply_defaults("read_artifact", args)
    path = context.run_path(args["path"])
    if not path.is_file():
        raise ValueError("path is not an artifact file")
    start = int(args["start"])
    end = int(args["end"])
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if start > len(lines):
        raise ValueError(f"start is past the end of the artifact ({len(lines)} lines)")
    # read_artifact 的输出通用层不会再卸载，但仍会被 clip() 硬切在 MAX_TOOL_OUTPUT——
    # 那是静默有损的。默认区间同样按预算自适应收窄，把"截断"换成如实报出的续读位置。
    return _render_line_range(
        "read_artifact", path.name, lines, start, end, fit=not end_explicit
    )


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

