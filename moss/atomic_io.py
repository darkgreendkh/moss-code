"""原子且持久的落盘。

为什么存在：
`os.replace` 保证"文件系统里不会出现半截文件"，但**不保证数据已经在盘上**。
Ctrl-C / OOM / 进程被杀的场景下 replace 就够了（内核缓冲仍在），断电或内核 panic
的场景下不够——只有 `fsync(file)` + `fsync(dir)` 之后才谈得上"不丢"。
session/run_store/write_file 三处原来各写了一份 tmp + replace，注释里都写着
"断电不丢"，而承诺其实没兑现。这里统一实现一次，把承诺补齐。

在链路里的位置：
纯 IO 底座，不认识 agent 的任何概念。`SessionStore` / `RunStore` /
`tools.write_text_atomic` 都走它。

降级必须显式：
Windows 打不开目录 fd，目录 fsync 无法完成。这时不假装持久——记一条降级并
在进程内提示一次，`report.json` 里也能看到。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# jsonl 追加写的 fsync 间隔。取舍写在这里：
# 每条都 fsync 会把 trace 变成同步 IO（1000 条事件要几秒，spec-07 的验收门槛是
# <200ms）。而 flush 已经足以扛住"进程被杀"——数据到了内核缓冲，进程没了也还在。
# fsync 防的是断电，那是罕见事故，摊到每 N 条足够。
FSYNC_INTERVAL = 20

# 已经发生过的降级：{名称: 说明}。进程级，一次运行里同一种只记一次。
_DEGRADATIONS = {}
_APPEND_COUNTS = {}


def degradations():
    """本进程发生过的持久化降级。report 会带上它，让"不持久"看得见。"""
    return [{"name": name, "detail": detail} for name, detail in sorted(_DEGRADATIONS.items())]


def reset_degradations():
    """仅供测试：清空进程级降级记录。"""
    _DEGRADATIONS.clear()


def note_degradation(name, detail, stream=None):
    if name in _DEGRADATIONS:
        return False
    _DEGRADATIONS[name] = str(detail)
    stream = stream if stream is not None else sys.stderr
    try:
        stream.write(f"warning: durability degraded ({name}): {detail}\n")
        stream.flush()
    except Exception:
        # 提示失败绝不能影响写入本身。
        pass
    return True


def fsync_file(handle):
    """把一个已打开文件的数据刷到盘上。返回是否成功。"""
    try:
        handle.flush()
        os.fsync(handle.fileno())
    except (OSError, ValueError, AttributeError) as exc:
        note_degradation("file_fsync_unsupported", f"{exc.__class__.__name__}: {exc}")
        return False
    return True


def fsync_dir(path):
    """把目录项本身刷到盘上（rename 的持久化靠它）。返回是否成功。

    Windows 无法以 O_RDONLY 打开目录，这里会抛 PermissionError——捕获后记一次
    降级，不重试也不假装成功。
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        note_degradation("dir_fsync_unsupported", f"{exc.__class__.__name__}: {exc}")
        return False
    try:
        os.fsync(fd)
    except OSError as exc:
        note_degradation("dir_fsync_unsupported", f"{exc.__class__.__name__}: {exc}")
        return False
    finally:
        os.close(fd)
    return True


def write_atomic(path, data, *, encoding="utf-8", fsync=True):
    """原子且持久地整份写一个文本文件。

    顺序不能变：写临时文件 -> fsync(临时文件) -> replace -> fsync(目录)。
    先 replace 后 fsync 内容，等于把"要么旧要么新"降级成"可能是空文件"。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            handle.write(str(data))
            if fsync:
                fsync_file(handle)
            temp_name = handle.name
        os.replace(temp_name, path)
        temp_name = ""
        if fsync:
            fsync_dir(path.parent)
    finally:
        # 中途出错时不留 .tmp 残渣：它们既占盘，又会让"目录里有什么"变得不可读。
        if temp_name and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return path


def write_json_atomic(path, payload, *, indent=2, sort_keys=True, ensure_ascii=False):
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii)
    return write_atomic(path, text + "\n")


def append_line(path, line, *, fsync_every=FSYNC_INTERVAL, force_fsync=False):
    """向 jsonl 追加一行。每条 flush，每 `fsync_every` 条 fsync 一次。

    `force_fsync` 用在"这一条之后可以安全地认为状态已落地"的时刻
    （checkpoint、run 收尾），把摊派的间隔提前结清。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = str(line)
    if not text.endswith("\n"):
        text += "\n"
    key = str(path)
    count = _APPEND_COUNTS.get(key, 0) + 1
    should_fsync = bool(force_fsync) or (fsync_every > 0 and count % int(fsync_every) == 0)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        if should_fsync:
            fsync_file(handle)
    _APPEND_COUNTS[key] = 0 if should_fsync else count
    return path


def truncate_partial_tail(path):
    """砍掉 jsonl 末尾那条没写完的记录，返回被丢弃的字节数。

    为什么必须砍：崩在写一半会留下一个没有换行的半截行。直接往后追加会把
    新记录粘在它后面，粘出来的那一行两条都读不出来——一次崩溃于是吃掉了
    崩溃之后的第一条事件。半截记录本身也无从恢复（它连一个完整 JSON 都不是），
    所以就地截断是唯一能让文件重新自洽的做法，且要记一条降级让它看得见。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) in (b"\n", b"\r"):
            return 0
    data = path.read_bytes()
    cut = data.rfind(b"\n")
    keep = cut + 1 if cut >= 0 else 0
    dropped = len(data) - keep
    with path.open("r+b") as handle:
        handle.truncate(keep)
        fsync_file(handle)
    note_degradation(
        "partial_jsonl_tail_dropped",
        f"{path.name}: dropped {dropped} bytes of an unfinished record",
    )
    return dropped


def read_last_line(path):
    """从文件末尾反向读最后一个非空行，不解析全文。

    为什么值得单独写：trace 的下一个序号只依赖最后一条事件，而按行读全文是
    O(n)，每条事件都读一遍就成了 O(n²)——1000 条事件的 run 会明显卡顿。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= 0:
        return ""
    chunk = 4096
    buffer = b""
    position = size
    with path.open("rb") as handle:
        while position > 0:
            step = min(chunk, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            stripped = buffer.rstrip(b"\n\r")
            if b"\n" in stripped or position == 0:
                break
    line = buffer.rstrip(b"\n\r").rsplit(b"\n", 1)[-1]
    return line.decode("utf-8", "replace").strip()
