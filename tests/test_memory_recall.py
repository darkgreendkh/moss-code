import math

from moss.memory.service import LayeredMemory, _tokenize
from moss.context.repository.retrieval import BM25Index


def _dcg(ranked_ids, relevant_id, limit=3):
    for rank, doc_id in enumerate(ranked_ids[:limit], start=1):
        if doc_id == relevant_id:
            return 1.0 / math.log2(rank + 1)
    return 0.0


def _old_overlap_rank(documents, query, limit=3):
    query_tokens = _tokenize(query)
    ranked = []
    for index, (doc_id, text) in enumerate(documents):
        overlap = len(query_tokens & _tokenize(text))
        if overlap:
            ranked.append(((overlap, index), doc_id))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [doc_id for _, doc_id in ranked[:limit]]


def test_bm25_recall_set_improves_ndcg_at_3_by_at_least_thirty_percent():
    documents = []
    queries = []
    for index in range(60):
        doc_id = f"fact-{index}"
        unique = f"needle_{index}"
        if index == 0:
            text = f"project setup {unique} exact answer"
        else:
            text = f"{unique} exact answer"
        documents.append((doc_id, text))
        if index < 10:
            queries.append((f"project setup {unique}", doc_id))
    for index in range(20):
        documents.append((f"distractor-{index}", "project setup generic background"))

    index = BM25Index(tokenize=_tokenize)
    for doc_id, text in documents:
        index.add(doc_id, {"text": text})

    old_score = sum(
        _dcg(_old_overlap_rank(documents, query), relevant_id)
        for query, relevant_id in queries
    ) / len(queries)
    new_score = sum(
        _dcg([hit.doc_id for hit in index.search(query, limit=3)], relevant_id)
        for query, relevant_id in queries
    ) / len(queries)

    assert old_score > 0
    assert new_score >= old_score * 1.30


def test_layered_memory_exposes_retrieval_score_breakdown():
    memory = LayeredMemory()
    memory.append_note(
        "Use uv run pytest for this project",
        tags=("pytest",),
        created_at="2026-08-05T10:00:00+00:00",
    )

    hits = memory.retrieval_candidates("pytest", limit=1)

    assert [note["text"] for note in hits] == ["Use uv run pytest for this project"]
    assert len(memory.last_retrieval_explain) == 1
    assert set(memory.last_retrieval_explain[0]) == {"doc_id", "score", "breakdown"}

