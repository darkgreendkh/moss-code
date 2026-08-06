"""run 索引与保留策略。

为什么存在（spec-07 §4.8）：
启动时要知道"有没有上次没跑完的 run"，原来的做法是 glob 全部
`*/task_state.json` 再逐个 json.loads。跑了一千次之后，每次启动都要开一千个
文件——这条路径在用户按下回车之前，纯属白等。

索引是一份 append-only 的摘要：`.moss/runs/index.jsonl`，一行一次状态更新，
读的时候按 run_id 折叠（后写的赢）。启动只读它，命中 running 的那几个才去
打开真正的 task_state.json。

保留策略也挂在这里：run 目录会无限增长，而其中绝大多数是几个月前的噪音。
过期的打包成 `<run_id>.jsonl.gz`（stdlib gzip，解开还是可 grep 的 jsonl），
**永不清理**三类：pinned、持有有效租约（还在跑）、被评测工件引用。
"""

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..atomic_io import append_line, write_atomic

INDEX_FILENAME = "index.jsonl"
ARCHIVE_SUFFIX = ".jsonl.gz"

DEFAULT_RETENTION_COUNT = 200
DEFAULT_RETENTION_DAYS = 30

# index.jsonl 的行数超过"实际 run 数 × 这个倍数"就紧凑化。
# 一次 run 会写好几行（started / finished / pin），2 倍是摊下来的合理上限。
INDEX_COMPACTION_FACTOR = 3

INDEX_FIELDS = ("run_id", "started_at", "status", "stop_reason", "task_summary", "cost_usd", "pinned")


def retention_limits(env=None):
    """保留策略的两个上限。`MOSS_RUN_RETENTION_COUNT` / `_DAYS` 可覆盖。"""
    env = os.environ if env is None else env

    def _int(name, default):
        raw = str(env.get(name, "") or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        # 0 或负数 = 不按这一维裁剪。允许显式关掉，但不许因为写错而清空历史。
        return value if value > 0 else None

    return _int("MOSS_RUN_RETENTION_COUNT", DEFAULT_RETENTION_COUNT), _int(
        "MOSS_RUN_RETENTION_DAYS", DEFAULT_RETENTION_DAYS
    )


def _parse_timestamp(text):
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class RunIndex:
    """`.moss/runs/index.jsonl` 的读写。"""

    def __init__(self, root):
        self.root = Path(root)

    @property
    def path(self):
        return self.root / INDEX_FILENAME

    def archive_path(self, run_id):
        return self.root / f"{run_id}{ARCHIVE_SUFFIX}"

    # ---- 读 ----

    def entries(self):
        """按 run_id 折叠后的索引，最近开始的排在前面。"""
        folded = {}
        for record in self._raw_records():
            run_id = str(record.get("run_id", ""))
            if not run_id:
                continue
            merged = folded.get(run_id, {})
            merged.update({key: record[key] for key in record if key in INDEX_FIELDS})
            merged["run_id"] = run_id
            folded[run_id] = merged
        return sorted(folded.values(), key=lambda item: str(item.get("started_at", "")), reverse=True)

    def get(self, run_id):
        for entry in self.entries():
            if entry["run_id"] == str(run_id):
                return entry
        return None

    def running_run_ids(self):
        from ..task_state import STATUS_RUNNING

        return [entry["run_id"] for entry in self.entries() if entry.get("status") == STATUS_RUNNING]

    def _raw_records(self):
        if not self.path.exists():
            return []
        records = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                records.append(json.loads(text))
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    continue
                raise
        return records

    # ---- 写 ----

    def record(self, run_id, **fields):
        """追加一条索引更新。只写传进来的字段，其余沿用之前那条。"""
        payload = {"run_id": str(run_id)}
        payload.update({key: value for key, value in fields.items() if key in INDEX_FIELDS})
        append_line(self.path, json.dumps(payload, sort_keys=True, ensure_ascii=False))
        self._compact_if_needed()
        return payload

    def set_pinned(self, run_id, pinned=True):
        return self.record(run_id, pinned=bool(pinned))

    def _compact_if_needed(self):
        raw = self._raw_records()
        entries = self.entries()
        if not entries or len(raw) <= len(entries) * INDEX_COMPACTION_FACTOR:
            return False
        self.rewrite(entries)
        return True

    def rewrite(self, entries):
        body = "".join(
            json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n" for entry in entries
        )
        write_atomic(self.path, body)
        return self.path

    def rebuild(self, run_dirs):
        """索引不存在（首次升级、被删掉）时扫一遍现有 run 目录重建。

        只在索引缺失时发生一次——这是"慢一次"换"以后每次启动都快"。
        """
        entries = []
        for directory in run_dirs:
            payload = self._read_task_state(directory)
            if payload is None:
                continue
            entries.append(
                {
                    "run_id": str(payload.get("run_id", directory.name)),
                    "started_at": self._started_at(directory),
                    "status": str(payload.get("status", "")),
                    "stop_reason": str(payload.get("stop_reason", "")),
                    "task_summary": str(payload.get("user_request", ""))[:200],
                    "cost_usd": None,
                    "pinned": False,
                }
            )
        entries.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        self.rewrite(entries)
        return entries

    @staticmethod
    def _read_task_state(directory):
        path = directory / "task_state.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _started_at(directory):
        try:
            stamp = directory.stat().st_mtime
        except OSError:
            return ""
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def expired_run_ids(entries, *, keep_count, keep_days, protected, now=None):
    """按"最近 N 个 / M 天"算出该归档的 run。protected 里的一律不动。

    两个维度是"或"的关系：只要还在最近 N 个里、或者还在 M 天内，就留着。
    保守方向是明确的——工件删错了没法恢复，多占点盘只是多占点盘。
    """
    protected = {str(item) for item in (protected or ())}
    now = now or datetime.now(timezone.utc)
    ordered = sorted(entries, key=lambda item: str(item.get("started_at", "")), reverse=True)
    expired = []
    for position, entry in enumerate(ordered):
        run_id = str(entry.get("run_id", ""))
        if not run_id or run_id in protected or entry.get("pinned"):
            continue
        recent_enough = keep_count is None or position < keep_count
        if recent_enough:
            continue
        started = _parse_timestamp(entry.get("started_at"))
        if keep_days is not None and started is not None and now - started <= timedelta(days=keep_days):
            continue
        expired.append(run_id)
    return expired


def archive_run_dir(run_dir, archive_path):
    """把一个 run 目录打包成 gzip 过的 jsonl，返回打包了几个文件。

    为什么是 jsonl.gz 而不是 tar：解开之后仍然一行一个文件、可以 grep、
    可以用同一套 json 工具读——run 工件的价值在于"能翻出来看"，
    换成二进制归档格式就把这个价值丢了。
    """
    run_dir = Path(run_dir)
    lines = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines.append(
            json.dumps(
                {"file": path.relative_to(run_dir).as_posix(), "text": text},
                ensure_ascii=False,
            )
        )
    body = "\n".join(lines) + ("\n" if lines else "")
    Path(archive_path).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
        handle.write(body)
    return len(lines)


def read_archive(archive_path):
    """读回一个归档，返回 {相对路径: 内容}。"""
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        body = handle.read()
    files = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        files[str(record.get("file", ""))] = str(record.get("text", ""))
    return files


def referenced_run_ids(paths):
    """评测工件里提到过的 run_id。被引用的 run 不能清理掉。

    评测报告里的数字要能追回原始工件，否则"可复现"就只是一句话。
    """
    found = set()
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        _collect_run_ids(payload, found)
    return found


def _collect_run_ids(node, found):
    if isinstance(node, dict):
        run_id = node.get("run_id")
        if isinstance(run_id, str) and run_id:
            found.add(run_id)
        for value in node.values():
            _collect_run_ids(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_run_ids(value, found)
