"""Versioned schema for durable memory events.

Records are immutable values. Updating or deleting a memory therefore creates a
new event instead of mutating an object that may already have been audited.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from ..clock import now


MEMORY_RECORD_SCHEMA_VERSION = 1
MEMORY_SCOPES = frozenset({"global", "project", "path", "session"})
MEMORY_TRUST_LEVELS = frozenset({"user", "model", "tool"})
MEMORY_STATUSES = frozenset({"active", "superseded", "needs_review", "tombstone"})


@dataclass(frozen=True)
class SourceRef:
    run_id: str
    event_seq: int | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    content_sha: str | None = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, payload):
        payload = dict(payload or {})
        return cls(
            run_id=str(payload.get("run_id", "")),
            event_seq=_optional_int(payload.get("event_seq")),
            path=_optional_str(payload.get("path")),
            line_start=_optional_int(payload.get("line_start")),
            line_end=_optional_int(payload.get("line_end")),
            content_sha=_optional_str(payload.get("content_sha")),
        )


@dataclass(frozen=True)
class MemoryRecord:
    schema_version: int
    id: str
    scope: str
    scope_key: str
    topic: str
    subject: str
    text: str
    tags: tuple[str, ...]
    trust: str
    source_refs: tuple[SourceRef, ...]
    created_at: str
    observed_at: str
    confidence: float
    status: str
    supersedes: tuple[str, ...]
    hit_count: int = 0
    used_count: int = 0
    legacy: bool = False

    def __post_init__(self):
        if self.schema_version != MEMORY_RECORD_SCHEMA_VERSION:
            raise ValueError(f"unsupported memory schema version: {self.schema_version}")
        if self.scope not in MEMORY_SCOPES:
            raise ValueError(f"invalid memory scope: {self.scope}")
        if self.trust not in MEMORY_TRUST_LEVELS:
            raise ValueError(f"invalid memory trust: {self.trust}")
        if self.status not in MEMORY_STATUSES:
            raise ValueError(f"invalid memory status: {self.status}")
        if not self.id.startswith("mem_") or len(self.id) != 16:
            raise ValueError("memory id must be mem_ followed by 12 hex characters")
        if not self.topic or not self.text:
            raise ValueError("memory topic and text must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory confidence must be in [0, 1]")
        if self.hit_count < 0 or self.used_count < 0:
            raise ValueError("memory counters must be non-negative")

    def to_dict(self):
        payload = asdict(self)
        payload["tags"] = list(self.tags)
        payload["source_refs"] = [source.to_dict() for source in self.source_refs]
        payload["supersedes"] = list(self.supersedes)
        return payload

    @classmethod
    def from_dict(cls, payload):
        payload = dict(payload or {})
        return cls(
            schema_version=int(payload.get("schema_version", 0)),
            id=str(payload.get("id", "")),
            scope=str(payload.get("scope", "")),
            scope_key=str(payload.get("scope_key", "")),
            topic=str(payload.get("topic", "")),
            subject=str(payload.get("subject", "")),
            text=str(payload.get("text", "")),
            tags=tuple(str(tag) for tag in payload.get("tags", []) if str(tag)),
            trust=str(payload.get("trust", "")),
            source_refs=tuple(SourceRef.from_dict(item) for item in payload.get("source_refs", [])),
            created_at=str(payload.get("created_at", "")),
            observed_at=str(payload.get("observed_at", "")),
            confidence=float(payload.get("confidence", 0.0)),
            status=str(payload.get("status", "")),
            supersedes=tuple(str(item) for item in payload.get("supersedes", []) if str(item)),
            hit_count=int(payload.get("hit_count", 0)),
            used_count=int(payload.get("used_count", 0)),
            legacy=bool(payload.get("legacy", False)),
        )


def _optional_str(value):
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value):
    if value in (None, ""):
        return None
    return int(value)


def _record_id(payload):
    stable = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "mem_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:12]


def make_record(
    *,
    scope,
    scope_key,
    topic,
    subject,
    text,
    tags=(),
    trust="model",
    source_refs=(),
    created_at=None,
    observed_at=None,
    confidence=1.0,
    status="active",
    supersedes=(),
    hit_count=0,
    used_count=0,
    legacy=False,
):
    created_at = str(created_at or now())
    observed_at = str(observed_at or created_at)
    identity = {
        "scope": str(scope),
        "scope_key": str(scope_key),
        "topic": str(topic).strip(),
        "subject": str(subject).strip(),
        "text": str(text).strip(),
        "tags": sorted({str(tag).strip() for tag in tags if str(tag).strip()}),
        "trust": str(trust),
        "source_refs": [source.to_dict() for source in source_refs],
        "observed_at": observed_at,
        "supersedes": list(supersedes),
    }
    return MemoryRecord(
        schema_version=MEMORY_RECORD_SCHEMA_VERSION,
        id=_record_id(identity),
        scope=identity["scope"],
        scope_key=identity["scope_key"],
        topic=identity["topic"],
        subject=identity["subject"],
        text=identity["text"],
        tags=tuple(identity["tags"]),
        trust=identity["trust"],
        source_refs=tuple(source_refs),
        created_at=created_at,
        observed_at=observed_at,
        confidence=float(confidence),
        status=str(status),
        supersedes=tuple(str(item) for item in supersedes),
        hit_count=int(hit_count),
        used_count=int(used_count),
        legacy=bool(legacy),
    )


__all__ = [
    "MEMORY_RECORD_SCHEMA_VERSION",
    "MEMORY_SCOPES",
    "MEMORY_STATUSES",
    "MEMORY_TRUST_LEVELS",
    "MemoryRecord",
    "SourceRef",
    "make_record",
]
