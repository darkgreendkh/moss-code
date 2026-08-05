"""Dependency-free lexical retrieval shared by repository and memory features.

The index is intentionally small and rebuilt from the current memory working set.
That keeps persistence concerns out of retrieval and avoids a second cache format.
"""

from collections import Counter
from dataclasses import dataclass
import math
import re
import time


FIELD_WEIGHTS = {
    "tag": 3.0,
    "tags": 3.0,
    "subject": 2.0,
    "path": 2.0,
    "text": 1.0,
}
_ASCII_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_CJK_PATTERN = re.compile(r"[㐀-䶿一-鿿]+")


@dataclass(frozen=True)
class Hit:
    doc_id: str
    score: float
    breakdown: dict


def _default_tokenize(text):
    tokens = [token.lower() for token in _ASCII_TOKEN_PATTERN.findall(str(text))]
    for run in _CJK_PATTERN.findall(str(text)):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


class BM25Index:
    def __init__(self, *, k1=1.2, b=0.75, tokenize=None, decay_days=7):
        self.k1 = float(k1)
        self.b = float(b)
        self.tokenize = tokenize or _default_tokenize
        self.decay_days = float(decay_days)
        self._documents = {}

    def _tokens(self, text):
        # 现有 memory tokenizer 返回 set；通用索引也接受 list/tuple。BM25 在这个
        # 小语料场景只关心“词是否在字段里”，刻意不奖励关键词堆砌。
        return tuple(str(token) for token in self.tokenize(str(text)) if str(token))

    def add(self, doc_id, fields, *, weight=1.0, ts=None):
        normalized_fields = {}
        for field, value in dict(fields or {}).items():
            if isinstance(value, (list, tuple, set)):
                text = " ".join(str(item) for item in value)
            else:
                text = str(value or "")
            normalized_fields[str(field)] = frozenset(self._tokens(text))
        self._documents[str(doc_id)] = {
            "fields": normalized_fields,
            "weight": max(0.0, float(weight)),
            "ts": None if ts is None else float(ts),
        }
        return self

    def search(self, query, *, limit=5, min_score=0.0, now=None):
        query_tokens = tuple(dict.fromkeys(self._tokens(query)))
        if not query_tokens or int(limit) <= 0 or not self._documents:
            return []

        document_tokens = {
            doc_id: set().union(*document["fields"].values()) if document["fields"] else set()
            for doc_id, document in self._documents.items()
        }
        lengths = {doc_id: len(tokens) for doc_id, tokens in document_tokens.items()}
        average_length = sum(lengths.values()) / max(1, len(lengths))
        frequencies = Counter(
            token
            for token in query_tokens
            for tokens in document_tokens.values()
            if token in tokens
        )
        reference = time.time() if now is None else float(now)
        hits = []
        document_count = len(self._documents)
        for doc_id, document in self._documents.items():
            raw_bm25 = 0.0
            boosted_bm25 = 0.0
            length = lengths[doc_id]
            normalization = self.k1 * (
                1.0 - self.b + self.b * length / max(average_length, 1.0)
            )
            for token in query_tokens:
                document_frequency = frequencies.get(token, 0)
                if not document_frequency or token not in document_tokens[doc_id]:
                    continue
                idf = math.log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
                term_score = idf * (self.k1 + 1.0) / (1.0 + normalization)
                boosts = [
                    FIELD_WEIGHTS.get(field, 1.0)
                    for field, tokens in document["fields"].items()
                    if token in tokens
                ]
                raw_bm25 += term_score
                boosted_bm25 += term_score * max(boosts or [1.0])
            if raw_bm25 <= 0:
                continue
            timestamp = document["ts"]
            if timestamp is None or self.decay_days <= 0:
                recency = 1.0
            else:
                age_days = max(0.0, reference - timestamp) / 86400.0
                recency = math.exp(-age_days / self.decay_days)
            trust = document["weight"]
            score = boosted_bm25 * recency * trust
            if score < float(min_score):
                continue
            hits.append(
                Hit(
                    doc_id=doc_id,
                    score=score,
                    breakdown={
                        "bm25": raw_bm25,
                        "field_boost": round(boosted_bm25 / raw_bm25, 12),
                        "recency": recency,
                        "trust": trust,
                    },
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id))
        return hits[: int(limit)]
