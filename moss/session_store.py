"""Session JSON persistence."""

import json
from pathlib import Path

from .atomic_io import write_atomic


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        # 原子写：先写同目录临时文件、fsync、再 os.replace 覆盖。
        # session 会在每次 record/checkpoint 时被整份重写，如果直接 write_text
        # 在写到一半时进程被杀（Ctrl-C、断电、OOM），就会留下半截 JSON，
        # 下次 load 直接抛异常——用户整个会话（history + memory + checkpoints）全丢。
        # 原子替换保证磁盘上要么是旧的完整版本，要么是新的完整版本；
        # fsync 由 atomic_io 统一负责，断电场景才真的不丢（见 moss/atomic_io.py）。
        path = self.path(session["id"])
        write_atomic(path, json.dumps(session, indent=2, ensure_ascii=False))
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
