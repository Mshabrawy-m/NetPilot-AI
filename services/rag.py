"""Offline document retrieval - RAG-lite over project knowledge (Slice 5a).

Deterministic TF-IDF retrieval using the already-installed scikit-learn. No
embeddings, no vector DB, no network, no new dependency - and, crucially, no
compiled extension that Windows App Control would block. The corpus is the
project's own Markdown (roadmaps, phase specs, delivery reports, runbooks), so
answers are grounded in *this* system's documentation rather than a model's
recall. Retrieval returns the exact source chunks with scores; it never
invents content.
"""
from __future__ import annotations

import pathlib
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
#: Where documentation is expected to live (additive; missing globs are fine).
DEFAULT_GLOBS = ("*.md", "docs/*.md", "lab/*.md", "runbooks/*.md")
#: Hard cap so the index stays tiny and the UI instant.
MAX_CHUNK_CHARS = 800


def discover_docs(root: pathlib.Path | None = None,
                  globs=DEFAULT_GLOBS) -> list[pathlib.Path]:
    root = root or PROJECT_ROOT
    found: list[pathlib.Path] = []
    for pattern in globs:
        found.extend(sorted(root.glob(pattern)))
    # de-duplicate, keep deterministic order
    seen: set[str] = set()
    unique: list[pathlib.Path] = []
    for p in found:
        key = str(p)
        if key not in seen and p.is_file():
            seen.add(key)
            unique.append(p)
    return unique


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _bounded(paragraph: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split an oversized paragraph on sentence boundaries so no chunk is lost."""
    if len(paragraph) <= limit:
        return [paragraph]
    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        if current and len(current) + len(sentence) + 1 > limit:
            chunks.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


def chunk_document(path: pathlib.Path) -> list[dict]:
    """Read one file and return ``{source, heading, text}`` chunks."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    chunks: list[dict] = []
    heading = path.name
    for paragraph in _split_paragraphs(text):
        line = paragraph.splitlines()[0].strip()
        if line.startswith("#"):
            heading = line.lstrip("# ").strip()
        for piece in _bounded(paragraph):
            chunks.append({"source": path.name, "heading": heading,
                           "text": piece})
    return chunks


def build_index(paths=None, *, root: pathlib.Path | None = None,
                globs=DEFAULT_GLOBS) -> dict:
    """Build a TF-IDF index over the given chunks.

    Returns ``{"ready": bool, "chunks": [...], "_vectorizer", "_matrix"}``.
    An empty corpus yields ``ready=False`` (search then returns nothing rather
    than raising). The vectorizer uses built-in English stopwords only - no
    external corpus download.
    """
    if paths is None:
        paths = discover_docs(root, globs)
    chunks: list[dict] = []
    for p in paths:
        chunks.extend(chunk_document(pathlib.Path(p)))
    if not chunks:
        return {"ready": False, "chunks": [], "_vectorizer": None,
                "_matrix": None}
    texts = [c["text"] for c in chunks]
    vectorizer = TfidfVectorizer(
        stop_words="english", ngram_range=(1, 2), sublinear_tf=True,
        min_df=1, token_pattern=r"(?u)\b\w[\w./-]+\b")
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError:
        # Whole corpus is stopword-only: nothing to index, honest no-match.
        return {"ready": False, "chunks": chunks, "_vectorizer": None,
                "_matrix": None}
    return {"ready": True, "chunks": chunks, "_vectorizer": vectorizer,
            "_matrix": matrix}


def search(index: dict, query: str, k: int = 3) -> list[dict]:
    """Top-``k`` chunks by cosine similarity. Empty/stopword-only queries or an
    unready index return ``[]`` (honest no-match, never a guess)."""
    if not index or not index.get("ready") or not (query or "").strip():
        return []
    vectorizer = index["_vectorizer"]
    matrix = index["_matrix"]
    try:
        qvec = vectorizer.transform([query])
    except ValueError:
        return []
    if qvec.nnz == 0:            # every query token was a stopword
        return []
    sims = linear_kernel(qvec, matrix)[0]
    order = sims.argsort()[::-1]
    results: list[dict] = []
    for idx in order[:max(1, k)]:
        score = float(sims[idx])
        if score <= 0.0:
            continue
        chunk = index["chunks"][int(idx)]
        results.append({**chunk, "score": round(score, 4)})
    return results
