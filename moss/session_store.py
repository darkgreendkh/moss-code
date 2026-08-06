"""会话持久化。

为什么是目录 + append-only（spec-07 §4.1）：
session 原来是单个 JSON，**每次 record() 整份重写**。一轮对话至少 record 好几次
（user / assistant / 每个工具结果），100 轮下来就是几百次全量重写，累计写入量
是 O(n²)——历史越长，每写一条越慢，而写的绝大部分内容和上一次一模一样。

v2 把"会变的一点点"和"只会追加的一大片"分开：
```
.moss/sessions/<id>/
  meta.json          # 小对象（memory / runtime_identity / 指针），整份原子写
  history.jsonl      # append-only，一条一行
  checkpoints.jsonl  # append-only + 定期紧凑化
```
`save(session)` 签名不变（调用点一行都不用改），内部改成增量写：能追加就追加，
只有在历史被截断/改写（/reset、rewind、compaction 重写）时才整份重写。

v1 单文件自动迁移，原文件留成 `<id>.json.v1bak`，迁移幂等。
"""

import hashlib
import json
from pathlib import Path

from .atomic_io import append_line, write_atomic

SESSION_SCHEMA_VERSION = 2

META_FILENAME = "meta.json"
HISTORY_FILENAME = "history.jsonl"
CHECKPOINTS_FILENAME = "checkpoints.jsonl"
LEGACY_BACKUP_SUFFIX = ".v1bak"

# checkpoints.jsonl 超过这个行数就紧凑化。取 CHECKPOINT_HISTORY_LIMIT 的两倍：
# 内存里最多留 40 条，磁盘上允许攒到 80 行再重写一次，摊下来每条 checkpoint
# 只多付 0.5 次全量重写。
CHECKPOINT_FILE_LIMIT = 80

# meta.json 里不放 history / checkpoints —— 它们各自有自己的 append-only 文件。
_SPLIT_KEYS = ("history", "checkpoints")


def _entry_digest(entry):
    text = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dump(entry):
    return json.dumps(entry, ensure_ascii=False, sort_keys=True)


def _read_jsonl(path):
    if not path.exists():
        return []
    entries = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        text = line.strip()
        if not text:
            continue
        try:
            entries.append(json.loads(text))
        except json.JSONDecodeError:
            # 末行可能是崩在写一半的半截 JSON。丢它一条，别让整个会话读不出来。
            if index == len(lines) - 1:
                continue
            raise
    return entries


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # {session_id: (已写入的条数, 最后一条的 digest)}。增量写靠它判断
        # "磁盘上那份是不是当前这份的前缀"。进程重启后为空 —— 那时第一次 save
        # 会重新扫一次磁盘，代价一次，之后照常追加。
        self._history_cursors = {}
        self._checkpoint_cursors = {}

    # ---- 路径 ----

    def path(self, session_id):
        """会话在 v2 布局下的位置（一个目录）。"""
        return self.root / str(session_id)

    def legacy_path(self, session_id):
        return self.root / f"{session_id}.json"

    def meta_path(self, session_id):
        return self.path(session_id) / META_FILENAME

    def history_path(self, session_id):
        return self.path(session_id) / HISTORY_FILENAME

    def checkpoints_path(self, session_id):
        return self.path(session_id) / CHECKPOINTS_FILENAME

    # ---- 写 ----

    def save(self, session):
        """整份 session 进来，增量落盘出去。返回会话目录。

        为什么保留"整份进来"的接口：runtime 手上本来就是一份完整的 session dict，
        让它去追踪"这次改了哪一段"只会把状态一致性的责任摊到调用方。
        """
        session_id = str(session["id"])
        directory = self.path(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.append_history(session_id, session.get("history", []))
        self.append_checkpoint(session_id, session.get("checkpoints", {}))
        self.save_meta(session_id, session)
        return directory

    def save_meta(self, session_id, session):
        """meta 很小（memory + 指针），整份原子写完全可接受。"""
        checkpoints = dict(session.get("checkpoints", {}) or {})
        meta = {key: value for key, value in session.items() if key not in _SPLIT_KEYS}
        meta["schema_version"] = SESSION_SCHEMA_VERSION
        meta["current_checkpoint_id"] = str(checkpoints.get("current_id", "") or "")
        write_atomic(self.meta_path(session_id), json.dumps(meta, indent=2, ensure_ascii=False))
        return self.meta_path(session_id)

    def append_history(self, session_id, history):
        """把 history 里还没落盘的部分追加进去；对不上前缀就整份重写。

        对不上的情况是真实存在的：/reset 清空历史、compaction 把一段历史换成
        一条摘要、rewind 截断到某个 checkpoint。这些都不是"追加"，只能重写。
        """
        history = list(history or [])
        path = self.history_path(session_id)
        written, last_digest = self._cursor(self._history_cursors, session_id, path)
        if written > len(history) or (written and _entry_digest(history[written - 1]) != last_digest):
            self._rewrite_jsonl(path, history)
            self._history_cursors[session_id] = (
                len(history),
                _entry_digest(history[-1]) if history else "",
            )
            return path
        for entry in history[written:]:
            append_line(path, _dump(entry))
        if history:
            self._history_cursors[session_id] = (len(history), _entry_digest(history[-1]))
        else:
            self._history_cursors[session_id] = (0, "")
        return path

    def append_checkpoint(self, session_id, checkpoints):
        """追加这一轮新增的 checkpoint；文件太长就按内存里那份紧凑化。"""
        items = dict((checkpoints or {}).get("items", {}) or {})
        path = self.checkpoints_path(session_id)
        written, _ = self._cursor(self._checkpoint_cursors, session_id, path)
        known = self._known_checkpoint_ids(session_id, path, written)
        appended = 0
        for checkpoint_id, payload in items.items():
            if checkpoint_id in known:
                continue
            append_line(path, _dump(payload), force_fsync=True)
            known.add(checkpoint_id)
            appended += 1
        total = written + appended
        if total > CHECKPOINT_FILE_LIMIT:
            # 内存里那份已经被 _prune_checkpoints 裁到 40 条了，直接照它重写。
            self._rewrite_jsonl(path, list(items.values()))
            total = len(items)
            self._checkpoint_ids = {session_id: set(items)}
        self._checkpoint_cursors[session_id] = (total, "")
        return path

    def _known_checkpoint_ids(self, session_id, path, written):
        cache = getattr(self, "_checkpoint_ids", None)
        if cache is None:
            cache = self._checkpoint_ids = {}
        known = cache.get(session_id)
        if known is None or len(known) != written:
            known = {
                str(entry.get("checkpoint_id", ""))
                for entry in _read_jsonl(path)
                if entry.get("checkpoint_id")
            }
            cache[session_id] = known
        return known

    def _cursor(self, cursors, session_id, path):
        cached = cursors.get(session_id)
        if cached is not None:
            return cached
        entries = _read_jsonl(path)
        cursor = (len(entries), _entry_digest(entries[-1]) if entries else "")
        cursors[session_id] = cursor
        return cursor

    @staticmethod
    def _rewrite_jsonl(path, entries):
        body = "".join(_dump(entry) + "\n" for entry in entries)
        write_atomic(path, body)
        return path

    # ---- 读 ----

    def load(self, session_id):
        session_id = str(session_id)
        self.migrate_if_needed(session_id)
        meta_path = self.meta_path(session_id)
        if not meta_path.exists():
            raise FileNotFoundError(str(meta_path))
        session = json.loads(meta_path.read_text(encoding="utf-8"))
        history = _read_jsonl(self.history_path(session_id))
        items = {}
        for entry in _read_jsonl(self.checkpoints_path(session_id)):
            checkpoint_id = str(entry.get("checkpoint_id", ""))
            if checkpoint_id:
                items[checkpoint_id] = entry
        session["history"] = history
        session["checkpoints"] = {
            "current_id": str(session.pop("current_checkpoint_id", "") or ""),
            "items": items,
        }
        session.pop("schema_version", None)
        # 装载即建游标：紧接着的第一次 save 就能直接追加，不用再扫一遍盘。
        self._history_cursors[session_id] = (
            len(history),
            _entry_digest(history[-1]) if history else "",
        )
        self._checkpoint_cursors[session_id] = (len(items), "")
        return session

    def migrate_if_needed(self, session_id):
        """v1 单文件 -> v2 目录。幂等：迁移过的再调一次什么也不做。

        原文件改名为 `<id>.json.v1bak` 而不是删除 —— 迁移是唯一一次有机会丢掉
        用户全部历史的操作，留一份原件的成本远低于赌它不出错。
        """
        legacy = self.legacy_path(session_id)
        if not legacy.exists():
            return False
        if self.meta_path(session_id).exists():
            # 已经有 v2 了：v1 文件是上一次迁移遗留的，直接归档掉。
            legacy.replace(Path(str(legacy) + LEGACY_BACKUP_SUFFIX))
            return False
        session = json.loads(legacy.read_text(encoding="utf-8"))
        expected = len(list(session.get("history", []) or []))
        self._history_cursors.pop(session_id, None)
        self._checkpoint_cursors.pop(session_id, None)
        getattr(self, "_checkpoint_ids", {}).pop(session_id, None)
        directory = self.path(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._rewrite_jsonl(self.history_path(session_id), session.get("history", []) or [])
        self._rewrite_jsonl(
            self.checkpoints_path(session_id),
            list(dict((session.get("checkpoints", {}) or {}).get("items", {}) or {}).values()),
        )
        self.save_meta(session_id, session)
        actual = len(_read_jsonl(self.history_path(session_id)))
        if actual != expected:
            raise RuntimeError(
                f"session migration lost history: {session_id} expected {expected}, got {actual}"
            )
        legacy.replace(Path(str(legacy) + LEGACY_BACKUP_SUFFIX))
        return True

    def latest(self):
        """按 mtime 最新的一个会话 id。v1 与 v2 布局同时识别。"""
        candidates = []
        for meta in self.root.glob(f"*/{META_FILENAME}"):
            candidates.append((meta.stat().st_mtime, meta.parent.name))
        for legacy in self.root.glob("*.json"):
            candidates.append((legacy.stat().st_mtime, legacy.stem))
        if not candidates:
            return None
        return max(candidates)[1]
