"""SQLite persistence layer.

SQLite is used so the project runs with zero infrastructure. The schema is written in
portable SQL (see docs/ARCHITECTURE.md) and maps directly onto PostgreSQL for production;
the FTS5 index would become a `tsvector` GIN index there.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id             TEXT PRIMARY KEY,
    filename       TEXT NOT NULL,
    file_type      TEXT NOT NULL,            -- pdf | image | spreadsheet
    mime_hint      TEXT,
    size_bytes     INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    storage_path   TEXT NOT NULL,
    status         TEXT NOT NULL,            -- queued | processing | completed | needs_review | failed
    error          TEXT,
    error_code     TEXT,
    page_count     INTEGER,
    source_method  TEXT,                     -- pdf_text | pdf_ocr | image_ocr | spreadsheet | csv
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS extracted_text (
    document_id     TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    ocr_confidence  REAL,
    warnings_json   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS invoices (
    document_id        TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    invoice_number     TEXT,
    invoice_number_norm TEXT,                -- upper-case alphanumerics only, for duplicate lookups
    invoice_date       TEXT,                 -- ISO 8601 (YYYY-MM-DD)
    supplier_name      TEXT,
    customer_name      TEXT,
    subtotal           REAL,
    tax                REAL,
    total              REAL,
    currency           TEXT,                 -- ISO 4217
    line_items_json    TEXT NOT NULL DEFAULT '[]',
    field_confidence_json TEXT NOT NULL DEFAULT '{}',
    raw_values_json    TEXT NOT NULL DEFAULT '{}',  -- unparseable source strings, e.g. {"total": "TBD"}
    extraction_method  TEXT,                 -- rules | llm | manual
    corrected_fields_json TEXT NOT NULL DEFAULT '[]',
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_number_norm ON invoices(invoice_number_norm);

CREATE TABLE IF NOT EXISTS validation_issues (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    rule         TEXT NOT NULL,
    severity     TEXT NOT NULL,              -- error | warning
    field        TEXT,
    message      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_doc ON validation_issues(document_id);

CREATE TABLE IF NOT EXISTS processing_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    status       TEXT NOT NULL,              -- ok | error | info
    message      TEXT,
    duration_ms  INTEGER,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,              -- queued | running | done | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    document_id  TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    vector_json  TEXT NOT NULL              -- sparse {index: weight} from a stateless hashing vectorizer
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    document_id UNINDEXED,
    filename,
    body,
    tokenize = 'porter unicode61'
);
"""

_lock = threading.RLock()
_initialised = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    global _initialised
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised = True


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction. Commits on success, rolls back on error."""
    if not _initialised:
        init_db()
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with tx() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def query_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
