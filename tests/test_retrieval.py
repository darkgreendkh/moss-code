from datetime import datetime, timezone

from moss.memory.service import _tokenize
from moss.retrieval import BM25Index


def test_bm25_prefers_concise_match_over_long_keyword_stuffing():
    index = BM25Index(tokenize=_tokenize)
    index.add("short", {"text": "cache key invalidation"})
    long_fillers = " ".join(f"filler_{index}" for index in range(120))
    index.add("long", {"text": f"cache key {long_fillers}"})

    hits = index.search("cache key", limit=2)

    assert [hit.doc_id for hit in hits] == ["short", "long"]


def test_bm25_uses_idf_and_field_boosts():
    index = BM25Index(tokenize=_tokenize)
    index.add("tagged", {"tag": "deploy", "text": "release checklist"})
    index.add("body", {"text": "deploy checklist"})
    index.add("noise", {"text": "checklist only"})

    hits = index.search("deploy", limit=3)

    assert [hit.doc_id for hit in hits] == ["tagged", "body"]
    assert hits[0].breakdown["field_boost"] == 3.0
    assert hits[0].breakdown["bm25"] > 0


def test_bm25_recalls_chinese_bigrams_and_explains_score():
    index = BM25Index(tokenize=_tokenize)
    index.add("cache", {"subject": "缓存键", "text": "稳定头哈希"}, weight=1.2)
    index.add("other", {"text": "数据库连接池"}, weight=0.8)

    hits = index.search("缓存键设计", limit=2)

    assert [hit.doc_id for hit in hits] == ["cache"]
    assert set(hits[0].breakdown) == {"bm25", "field_boost", "recency", "trust"}
    assert hits[0].breakdown["trust"] == 1.2


def test_bm25_applies_time_decay_and_min_score_abstention():
    reference = datetime(2026, 8, 5, tzinfo=timezone.utc).timestamp()
    index = BM25Index(tokenize=_tokenize, decay_days=7)
    index.add("fresh", {"text": "pytest convention"}, ts=reference)
    index.add("stale", {"text": "pytest convention"}, ts=reference - 14 * 86400)

    hits = index.search("pytest", limit=2, now=reference)

    assert [hit.doc_id for hit in hits] == ["fresh", "stale"]
    assert hits[0].breakdown["recency"] == 1.0
    assert 0 < hits[1].breakdown["recency"] < hits[0].breakdown["recency"]
    assert index.search("missing-term", min_score=0.01, now=reference) == []
    assert index.search("pytest", min_score=hits[0].score + 0.01, now=reference) == []
