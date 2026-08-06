"""Atomic append-only durable-memory storage and Markdown projections."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from ..clock import now
from .records import MemoryRecord, make_record


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
_SUBJECT_TOKEN_PATTERN = re.compile(r"[a-z0-9_./-]+|[\u3400-\u9fff]+", re.I)
_SUBJECT_STOP_WORDS = frozenset({"a", "an", "the", "is", "are", "be", "to", "的", "是", "使用"})
_TRUST_RANK = {"tool": 0, "model": 1, "user": 2}
_NEGATION_PATTERN = re.compile(r"\b(?:not|never|no|disabled?|forbidden)\b|(?:不|禁止|不可|禁用)", re.I)


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


def _normalize_subject_text(text):
    tokens = [
        token.lower()
        for token in _SUBJECT_TOKEN_PATTERN.findall(str(text))
        if token.lower() not in _SUBJECT_STOP_WORDS
    ]
    return " ".join(tokens[:6])


def _subject_for(text, aliases=None):
    text = str(text).strip()
    for pattern in _SUBJECT_PATTERNS:
        match = pattern.match(text)
        if match:
            subject = _normalize_subject_text(match.group(1))
            return (aliases or {}).get(subject, subject)
    subject = _normalize_subject_text(text)
    return (aliases or {}).get(subject, subject)


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _texts_conflict(first, second):
    first_text = " ".join(str(first).lower().split())
    second_text = " ".join(str(second).lower().split())
    if first_text == second_text:
        return False
    if bool(_NEGATION_PATTERN.search(first_text)) != bool(_NEGATION_PATTERN.search(second_text)):
        return True
    split_pattern = re.compile(r"\s+(?:is|are|uses?|should)\s+|是|使用", re.I)
    first_parts = split_pattern.split(first_text, maxsplit=1)
    second_parts = split_pattern.split(second_text, maxsplit=1)
    return len(first_parts) == len(second_parts) == 2 and first_parts[1] != second_parts[1]


def _rebuild_record(record, *, subject=None, supersedes=None, status=None):
    return make_record(
        scope=record.scope,
        scope_key=record.scope_key,
        topic=record.topic,
        subject=record.subject if subject is None else subject,
        text=record.text,
        tags=record.tags,
        trust=record.trust,
        source_refs=record.source_refs,
        created_at=record.created_at,
        observed_at=record.observed_at,
        confidence=record.confidence,
        status=record.status if status is None else status,
        supersedes=record.supersedes if supersedes is None else supersedes,
        hit_count=record.hit_count,
        used_count=record.used_count,
        legacy=record.legacy,
    )


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
        self.aliases_path = self.root / "aliases.md"

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
            # 不转义非 ASCII：records.jsonl 是事实源，人要能直接 grep/读。
            # 记忆 id 的稳定摘要另有一套 canonical 形式（memory/records.py），不受这里影响。
            json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"
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

    def recallable_records(self):
        return [record for record in self.all_records() if record.status in {"active", "needs_review"}]

    def aliases(self):
        if not self.aliases_path.exists():
            return {}
        aliases = {}
        for raw in self.aliases_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip().lstrip("- ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            terms = [_normalize_subject_text(term) for term in line.split("=")]
            terms = [term for term in terms if term]
            if not terms:
                continue
            canonical = terms[0]
            for term in terms:
                aliases[term] = canonical
        return aliases

    def subject_for(self, text):
        return _subject_for(text, self.aliases())

    def append_resolving_conflicts(self, record):
        """Append a new fact while preserving one active winner per subject.

        Equal-trust contradictions observed within one hour stay queryable as
        `needs_review`; otherwise trust wins first, then observation time.
        """
        record = _rebuild_record(record, subject=self.subject_for(record.text))
        peers = [
            old
            for old in self.active_records()
            if old.scope == record.scope
            and old.scope_key == record.scope_key
            and old.topic == record.topic
            and self.subject_for(old.subject) == record.subject
        ]
        if not peers:
            self.append(record)
            return record
        old = max(peers, key=lambda item: (_parse_time(item.observed_at), item.created_at))
        old_rank = _TRUST_RANK.get(old.trust, 0)
        new_rank = _TRUST_RANK.get(record.trust, 0)
        seconds_apart = abs(_parse_time(record.observed_at) - _parse_time(old.observed_at))
        if new_rank == old_rank and seconds_apart < 3600 and _texts_conflict(old.text, record.text):
            reviewed = replace(record, status="needs_review")
            self.append_many([replace(old, status="needs_review"), reviewed])
            return reviewed
        if new_rank > old_rank or (
            new_rank == old_rank and _parse_time(record.observed_at) > _parse_time(old.observed_at)
        ):
            replacement = _rebuild_record(record, supersedes=(old.id,))
            self.append_many([replacement, replace(old, status="superseded")])
            return replacement
        superseded = replace(record, status="superseded")
        self.append(superseded)
        return superseded

    def refresh_freshness(self):
        if self.workspace_root is None:
            return []
        stale = []
        root = self.workspace_root.resolve()
        for record in self.active_records():
            for source in record.source_refs:
                if not source.path or not source.content_sha:
                    continue
                path = (root / source.path).resolve()
                try:
                    path.relative_to(root)
                    current_sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
                except (OSError, ValueError):
                    current_sha = ""
                if current_sha != source.content_sha:
                    stale.append(replace(record, status="needs_review"))
                    break
        if stale:
            self.append_many(stale)
        return stale

    def cold_path(self, session_id):
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "session")).strip(".-")
        return self.episodic_dir / f"{safe_id or 'session'}.jsonl"

    def append_cold_notes(self, session_id, notes):
        notes = [dict(note) for note in notes]
        if not notes:
            return []
        path = self.cold_path(session_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        payload = existing + "".join(
            json.dumps(note, ensure_ascii=False, sort_keys=True) + "\n" for note in notes
        )
        _atomic_write(path, payload)
        return notes

    def load_cold_notes(self, session_id=None):
        paths = [self.cold_path(session_id)] if session_id is not None else sorted(self.episodic_dir.glob("*.jsonl")) if self.episodic_dir.exists() else []
        notes = []
        for path in paths:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    note = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(note, dict) and str(note.get("text", "")).strip():
                    notes.append(note)
        return notes

    def append_procedural(self, record):
        if record.topic != "procedural":
            raise ValueError("procedural memory must use the procedural topic")
        duplicate = next(
            (
                existing
                for existing in self.recallable_records()
                if existing.topic == "procedural" and existing.text == record.text
            ),
            None,
        )
        if duplicate is not None:
            return duplicate
        self.append(record)
        sources = ", ".join(
            f"{source.run_id}#{source.event_seq}" if source.event_seq is not None else source.run_id
            for source in record.source_refs
        )
        body = "\n".join(
            [
                f"# Procedural Memory {record.id}",
                "",
                f"- trust: {record.trust}",
                f"- source_refs: {sources}",
                f"- observed_at: {record.observed_at}",
                "",
                record.text,
                "",
            ]
        )
        _atomic_write(self.procedural_dir / f"{record.id}.md", body)
        return record

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
        return sorted({record.topic for record in self.recallable_records()})

    def all_notes(self):
        self.refresh_freshness()
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
                "scope_key": record.scope_key,
                "source_refs": [source.to_dict() for source in record.source_refs],
                "review_reason": self._review_reason(record),
            }
            for record in self.recallable_records()
        ]

    def _review_reason(self, record):
        if record.status != "needs_review":
            return ""
        if self.workspace_root is not None:
            root = self.workspace_root.resolve()
            for source in record.source_refs:
                if not source.path or not source.content_sha:
                    continue
                path = (root / source.path).resolve()
                try:
                    path.relative_to(root)
                    current_sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
                except (OSError, ValueError):
                    current_sha = ""
                if current_sha != source.content_sha:
                    return "stale"
        return "conflict"

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
            subject = self.subject_for(text)
            old = next(
                (
                    record
                    for record in self.active_records()
                    if record.topic == topic and record.subject == subject
                ),
                None,
            )
            tags = old.tags if old is not None else TOPIC_METADATA.get(
                topic, (topic.replace("-", " ").title(), "", (topic,))
            )[2]
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
            new = self.append_resolving_conflicts(new)
            if old is not None and old.id in new.supersedes:
                superseded.append(f"{topic}: {old.text} -> {new.text}")
            promoted.append(f"{topic}: {new.text}")
        return promoted, superseded

    def rebuild_projections(self):
        active = self.recallable_records()
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
                        subject=self.subject_for(text),
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
