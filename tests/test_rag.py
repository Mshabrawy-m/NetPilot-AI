"""Offline tests for the TF-IDF document retriever (Slice 5a)."""
from services import rag


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_search_returns_relevant_chunk(tmp_path):
    _write(tmp_path, "a.md",
           "# Alerting\nAlert rules fire when a metric crosses a threshold "
           "and are deduplicated while open.\n\n"
           "# Forecasting\nForecasts use a chronological train val test split.\n")
    index = rag.build_index(paths=list(tmp_path.glob("*.md")))
    assert index["ready"] is True
    hits = rag.search(index, "when does an alert fire", k=2)
    assert hits, "expected at least one match"
    assert "threshold" in hits[0]["text"].lower()
    assert hits[0]["score"] > 0


def test_empty_corpus_is_not_ready():
    index = rag.build_index(paths=[])
    assert index["ready"] is False
    assert rag.search(index, "anything") == []


def test_stopword_only_query_returns_no_match(tmp_path):
    _write(tmp_path, "b.md", "# T\nthe and of are\n")
    index = rag.build_index(paths=list(tmp_path.glob("*.md")))
    assert rag.search(index, "the of and") == []


def test_retrieval_is_deterministic(tmp_path):
    _write(tmp_path, "c.md",
           "# QoS\nPriority queues map DSCP values to egress classes.\n")
    paths = list(tmp_path.glob("*.md"))
    r1 = rag.search(rag.build_index(paths=paths), "priority queue dscp")
    r2 = rag.search(rag.build_index(paths=paths), "priority queue dscp")
    assert r1 == r2 and r1


def test_overlong_paragraph_is_fully_chunked(tmp_path):
    long_para = "Sentence one. " * 200  # comfortably over MAX_CHUNK_CHARS
    _write(tmp_path, "d.md", "# Long\n" + long_para.strip() + "\n")
    chunks = rag.chunk_document(tmp_path / "d.md")
    joined = " ".join(c["text"] for c in chunks)
    assert "Sentence one." in joined
    assert all(len(c["text"]) <= rag.MAX_CHUNK_CHARS + 1 for c in chunks)
