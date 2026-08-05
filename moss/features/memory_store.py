"""Atomic append-only durable-memory storage and Markdown projections."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from ..clock import now
from .memory_records import MemoryRecord, make_record


DEFAULT_COMPACT_THRESHOLD = 2000
TOPIC_METADATA = {
    "project-conventions": ("Project Conventions", "Stable repository conventions.", ("convention",)),
    "key-decisions": ("Key Decisions", "Long-lived decisions and rationale anchors.", ("decision",)),
    "dependency-facts": ("Dependency Facts", "Stable dependency and environment facts.", ("dependency",)),
    "user-preferences": ("User Preferences", "Stable user preferences.", ("preference",)),
}
_SUBJECT_PATTERNS = (
    re.compile(r"^(.+?)\s+is\s+.+$", re.I),
    re.compile(r"^(.+?)\s+are\s+.+$", re.I),
    re.compile(r"^(.+?)\s+uses?\s+.+$", re.I),
    re.compile(r"^(.+?)\s+should\s+.+$", re.I),
    re.compile(r"^(.+?)是.+$"),
    re.compile(r"^(.+?)使用.+$"),
)


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            handle.write(str(text))
            temp_name = handle.name
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def project_scope_key(workspace_root):
    root = Path(workspace_root).resolve()
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _subject_for(text):
    text = str(text).strip()
    for pattern in _SUBJECT_PATTERNS:
        match = pattern.match(text)
        if match:
            return " ".join(match.group(1).lower().split())
    return " ".join(text.lower().split())[:120]


class MemoryStore:
    def __init__(self, root, *, workspace_root=None, compact_threshold=DEFAULT_COMPACT_THRESHOLD):
        self.root = Path(root)
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self.compact_threshold = max(1, int(compact_threshold))
        self.records_path = self.root / "records.jsonl"
        self.index_path = self.root / "MEMORY.md"
        self.topics_dir = self.root / "topics"
        self.procedural_dir = self.root / "procedural"
        self.episodic_dir = self.root / "episodic"

    def _read_events(self):
        if not self.records_path.exists():
            return []
        lines = self.records_path.read_text(encoding="utf-8").splitlines()
        records = []
        nonempty = [index for index, line in enumerate(lines) if line.strip()]
        last_nonempty = nonempty[-1] if nonempty else -1
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                records.append(MemoryRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError):
                if index == last_nonempty:
                    # 进程在旧式 append 中间被杀时，只忽略最后半行；完整上一代仍可用。
                    continue
                raise
        return records

    @staticmethod
    def _serialize(records):
        if not records:
            return ""
        return "".join(
            json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=True) + "\n"
            for record in records
        )

    def _persist(self, records):
        _atomic_write(self.records_path, self._serialize(records))

    @staticmethod
    def _validate_durable_record(record):
        if not isinstance(record, MemoryRecord):
            raise TypeError("record must be a MemoryRecord")
        if record.trust == "tool":
            raise ValueError("tool-derived content cannot be durable")
        if not record.legacy and not record.source_refs:
            raise ValueError("non-legacy durable memory requires source_refs")

    def append(self, record, *, rebuild=True):
        self._validate_durable_record(record)
        events = self._read_events()
        events.append(record)
        self._persist(events)
        if len(events) > self.compact_threshold:
            self.compact(rebuild=False)
        if rebuild:
            self.rebuild_projections()
        return record

    def append_many(self, records, *, rebuild=True):
        records = list(records)
        if not records:
            return []
        for record in records:
            self._validate_durable_record(record)
        events = self._read_events()
        events.extend(records)
        self._persist(events)
        if len(events) > self.compact_threshold:
            self.compact(rebuild=False)
        if rebuild:
            self.rebuild_projections()
        return records

    def all_records(self):
        latest = {}
        for record in self._read_events():
            latest[record.id] = record
        return list(latest.values())

    def active_records(self):
        return [record for record in self.all_records() if record.status == "active"]

    def get(self, record_id):
        return next((record for record in self.all_records() if record.id == str(record_id)), None)

    def update(self, record_id, text, *, observed_at=None, source_refs=None, trust=None):
        old = self.get(record_id)
        if old is None or old.status != "active":
            raise KeyError(f"unknown active memory: {record_id}")
        new = make_record(
            scope=old.scope,
            scope_key=old.scope_key,
            topic=old.topic,
            subject=old.subject,
            text=str(text).strip(),
            tags=old.tags,
            trust=trust or old.trust,
            source_refs=tuple(source_refs) if source_refs is not None else old.source_refs,
            created_at=now(),
            observed_at=observed_at or now(),
            confidence=old.confidence,
            supersedes=(old.id,),
            legacy=old.legacy,
        )
        self.append_many([new, replace(old, status="superseded")])
        return new

    def delete(self, record_id):
        old = self.get(record_id)
        if old is None:
            raise KeyError(f"unknown memory: {record_id}")
        tombstone = replace(old, status="tombstone")
        self.append(tombstone)
        return tombstone

    def compact(self, *, rebuild=True):
        compacted = self.all_records()
        self._persist(compacted)
        if rebuild:
            self.rebuild_projections()
        return len(compacted)

    def topic_slugs(self):
        return sorted({record.topic for record in self.active_records()})

    def all_notes(self):
        return [
            {
                "id": record.id,
                "text": record.text,
                "tags": list(record.tags),
                "source": record.topic,
                "created_at": record.observed_at,
                "kind": "durable",
                "trust": record.trust,
                "subject": record.subject,
                "status": record.status,
                "scope": record.scope,
                "source_refs": [source.to_dict() for source in record.source_refs],
            }
            for record in self.active_records()
        ]

    def promote(self, promotions, *, source_refs=(), trust="user"):
        promoted = []
        superseded = []
        scope_key = project_scope_key(self.workspace_root or self.root)
        for topic, text in promotions:
            text = str(text).strip()
            if not text:
                continue
            exact = next((record for record in self.active_records() if record.text == text), None)
            if exact is not None:
                continue
            subject = _subject_for(text)
            old = next(
                (
                    record
                    for record in self.active_records()
                    if record.topic == topic and record.subject == subject
                ),
                None,
            )
            if old is not None:
                new = self.update(old.id, text, source_refs=source_refs, trust=trust)
                superseded.append(f"{topic}: {old.text} -> {new.text}")
            else:
                tags = TOPIC_METADATA.get(topic, (topic.replace("-", " ").title(), "", (topic,)))[2]
                new = make_record(
                    scope="project",
                    scope_key=scope_key,
                    topic=topic,
                    subject=subject,
                    text=text,
                    tags=tags,
                    trust=trust,
                    source_refs=tuple(source_refs),
                )
                self.append(new)
            promoted.append(f"{topic}: {new.text}")
        return promoted, superseded

    def rebuild_projections(self):
        active = self.active_records()
        grouped = {}
        for record in active:
            grouped.setdefault(record.topic, []).append(record)
        if self.topics_dir.exists():
            # 投影不是事实源。某个 topic 的最后一条记录被 tombstone 后，旧 Markdown
            # 必须一起消失，否则人和 CLI 仍会看到一条已经 forget 的记忆。
            for path in self.topics_dir.glob("*.md"):
                if path.stem not in grouped:
                    path.unlink()
        index_lines = ["# Durable Memory Index", ""]
        for topic in sorted(grouped):
            title, summary, default_tags = TOPIC_METADATA.get(
                topic,
                (topic.replace("-", " ").title(), f"Durable memories for {topic}.", (topic,)),
            )
            tags = sorted({*default_tags, *(tag for record in grouped[topic] for tag in record.tags)})
            index_lines.extend(
                [
                    f"- [{topic}](topics/{topic}.md): {title}",
                    f"  - summary: {summary}",
                    f"  - tags: {', '.join(tags)}",
                ]
            )
            topic_lines = [
                f"# {title}",
                "",
                f"- topic: {topic}",
                f"- summary: {summary}",
                f"- tags: {', '.join(tags)}",
                f"- updated_at: {max(record.observed_at for record in grouped[topic])}",
                "",
                "## Notes",
            ]
            topic_lines.extend(f"- {record.text}" for record in grouped[topic])
            _atomic_write(self.topics_dir / f"{topic}.md", "\n".join(topic_lines).rstrip() + "\n")
        _atomic_write(self.index_path, "\n".join(index_lines).rstrip() + "\n")
        return self.index_path

    def migrate_legacy(self):
        if self.records_path.exists():
            return 0
        topic_paths = sorted(self.topics_dir.glob("*.md")) if self.topics_dir.exists() else []
        if not self.index_path.exists() and not topic_paths:
            return 0
        if self.index_path.exists():
            backup = self.root / "MEMORY.md.bak"
            if not backup.exists():
                _atomic_write(backup, self.index_path.read_text(encoding="utf-8"))
        scope_key = project_scope_key(self.workspace_root or self.root)
        records = []
        for path in topic_paths:
            topic, tags, observed_at, notes = self._parse_legacy_topic(path)
            for text in notes:
                records.append(
                    make_record(
                        scope="project",
                        scope_key=scope_key,
                        topic=topic,
                        subject=_subject_for(text),
                        text=text,
                        tags=tags,
                        trust="user",
                        source_refs=(),
                        created_at=observed_at,
                        observed_at=observed_at,
                        legacy=True,
                    )
                )
        self.append_many(records, rebuild=False) if records else self._persist([])
        self.rebuild_projections()
        return len(records)

    @staticmethod
    def _parse_legacy_topic(path):
        topic = path.stem
        tags = []
        observed_at = ""
        notes = []
        capture = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("- topic:"):
                topic = line.split(":", 1)[1].strip() or topic
            elif line.startswith("- tags:"):
                tags = [tag.strip() for tag in line.split(":", 1)[1].split(",") if tag.strip()]
            elif line.startswith("- updated_at:"):
                observed_at = line.split(":", 1)[1].strip()
            elif line == "## Notes":
                capture = True
            elif capture and line.startswith("- "):
                notes.append(line[2:].strip())
        return topic, tuple(tags), observed_at or now(), notes


__all__ = ["DEFAULT_COMPACT_THRESHOLD", "MemoryStore", "project_scope_key"]
