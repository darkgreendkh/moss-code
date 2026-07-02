"""Session JSON persistence."""

import json
import os
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        # 原子写：先写同目录临时文件再 os.replace 覆盖。
        # session 会在每次 record/checkpoint 时被整份重写，如果直接 write_text
        # 在写到一半时进程被杀（Ctrl-C、断电、OOM），就会留下半截 JSON，
        # 下次 load 直接抛异常——用户整个会话（history + memory + checkpoints）全丢。
        # 原子替换保证磁盘上要么是旧的完整版本，要么是新的完整版本。
        path = self.path(session["id"])
        data = json.dumps(session, indent=2, ensure_ascii=False)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(self.root),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            handle.write(data)
            temp_name = handle.name
        os.replace(temp_name, path)
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
