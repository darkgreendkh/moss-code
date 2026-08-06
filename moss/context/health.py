"""Pure context-health metrics used by prompt assembly and reports."""

from .repository.retrieval import BM25Index


def distractor_ratio(rendered, user_message, measure):
    chunks = []
    for section in ("history", "memory", "relevant_memory"):
        if section not in rendered:
            continue
        for index, line in enumerate(str(rendered[section].rendered).splitlines()):
            if line.strip():
                chunks.append((f"{section}:{index}", line))
    if not chunks:
        return 0.0
    index = BM25Index(decay_days=0)
    for doc_id, line in chunks:
        index.add(doc_id, {"text": line})
    relevant = {
        hit.doc_id
        for hit in index.search(user_message, limit=len(chunks), min_score=0.0)
    }
    total = sum(measure(line) for _, line in chunks) or 1
    distracting = sum(measure(line) for doc_id, line in chunks if doc_id not in relevant)
    return round(distracting / total, 4)


def context_health(
    prompt,
    rendered,
    section_metadata,
    user_message,
    *,
    measure,
    context_window,
    history_entries,
):
    total_units = max(1, measure(prompt))
    window = max(1, int(context_window))
    shares = {
        section: round(int(data.get("rendered_units", 0)) / total_units, 4)
        for section, data in section_metadata.items()
    }
    return {
        "context_utilization": round(total_units / window, 4),
        "context_window": window,
        "section_share": shares,
        "distractor_ratio": distractor_ratio(rendered, user_message, measure),
        "history_staleness": len(history_entries),
    }
