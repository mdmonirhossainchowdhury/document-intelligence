"""Search over processed documents.

keyword  -> SQLite FTS5 (BM25 ranking, Porter stemming, highlighted snippets)
semantic -> sparse TF-IDF-style vectors from a *stateless* HashingVectorizer (1-2 grams, English
            stop-words) stored per document and compared by cosine similarity. Stateless hashing
            means no vocabulary has to be re-fitted as documents arrive. For production, swap for
            dense sentence embeddings stored in pgvector / a vector DB (see docs/ARCHITECTURE.md).
hybrid   -> reciprocal-rank fusion of the two rankings.
"""
from __future__ import annotations

import json
import re

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import HashingVectorizer

from . import db

N_FEATURES = 2 ** 18
MAX_CHARS = 30_000
_vectorizer = HashingVectorizer(n_features=N_FEATURES, ngram_range=(1, 2), stop_words="english",
                                alternate_sign=False, norm="l2")


def _document_body(doc_id: str) -> tuple[str, str] | None:
    doc = db.query_one("SELECT filename FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        return None
    text_row = db.query_one("SELECT text FROM extracted_text WHERE document_id = ?", (doc_id,))
    inv = db.query_one(
        "SELECT invoice_number, supplier_name, customer_name, currency FROM invoices WHERE document_id = ?", (doc_id,)
    )
    parts = [text_row["text"] if text_row else ""]
    if inv:
        parts.append(" ".join(str(v) for v in inv.values() if v))
    body = "\n".join(parts)[:MAX_CHARS]
    return doc["filename"], body


def index_document(doc_id: str) -> None:
    """(Re)build the keyword and semantic index entries for one document."""
    loaded = _document_body(doc_id)
    if loaded is None:
        return
    filename, body = loaded
    vector = _vectorizer.transform([f"{filename}\n{body}"])
    sparse = {int(i): float(v) for i, v in zip(vector.indices, vector.data)}
    with db.tx() as conn:
        conn.execute("DELETE FROM documents_fts WHERE document_id = ?", (doc_id,))
        conn.execute("INSERT INTO documents_fts(document_id, filename, body) VALUES (?, ?, ?)", (doc_id, filename, body))
        conn.execute(
            "INSERT INTO embeddings(document_id, vector_json) VALUES (?, ?) "
            "ON CONFLICT(document_id) DO UPDATE SET vector_json = excluded.vector_json",
            (doc_id, json.dumps(sparse)),
        )


def remove_document(doc_id: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM documents_fts WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM embeddings WHERE document_id = ?", (doc_id,))


def _fts_query(q: str) -> str:
    tokens = re.findall(r"[\w\-/.]+", q, flags=re.UNICODE)
    return " AND ".join(f'"{t.replace(chr(34), "")}"' for t in tokens[:25])


def keyword_search(q: str, limit: int) -> list[dict]:
    match = _fts_query(q)
    if not match:
        return []
    rows = db.query_all(
        """SELECT document_id, bm25(documents_fts) AS rank,
                  snippet(documents_fts, 2, '<mark>', '</mark>', '…', 12) AS snippet
           FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?""",
        (match, limit),
    )
    return [{"document_id": r["document_id"], "score": round(-float(r["rank"]), 4), "snippet": r["snippet"]} for r in rows]


def semantic_search(q: str, limit: int) -> list[dict]:
    rows = db.query_all("SELECT document_id, vector_json FROM embeddings")
    if not rows:
        return []
    query = _vectorizer.transform([q])
    if query.nnz == 0:
        return []
    data, indices, indptr = [], [], [0]
    for r in rows:
        vec = json.loads(r["vector_json"])
        indices.extend(vec.keys())
        data.extend(vec.values())
        indptr.append(len(indices))
    matrix = csr_matrix((np.array(data), np.array(indices), np.array(indptr)), shape=(len(rows), N_FEATURES))
    scores = (matrix @ query.T).toarray().ravel()
    order = np.argsort(-scores)[:limit]
    return [{"document_id": rows[i]["document_id"], "score": round(float(scores[i]), 4), "snippet": None}
            for i in order if scores[i] > 0]


def hybrid_search(q: str, limit: int, k: int = 60) -> list[dict]:
    fused: dict[str, float] = {}
    first: dict[str, dict] = {}
    for ranking in (keyword_search(q, limit * 3), semantic_search(q, limit * 3)):
        for rank, hit in enumerate(ranking, start=1):
            fused[hit["document_id"]] = fused.get(hit["document_id"], 0.0) + 1.0 / (k + rank)
            first.setdefault(hit["document_id"], hit)
    ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:limit]
    return [{**first[doc_id], "score": round(score, 6)} for doc_id, score in ordered]


def search(q: str, mode: str, limit: int) -> list[dict]:
    if mode == "keyword":
        return keyword_search(q, limit)
    if mode == "semantic":
        return semantic_search(q, limit)
    return hybrid_search(q, limit)
