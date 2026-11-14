"""Document processing service: runs the pipeline for one document and persists the results.

Called by the background job runner (jobs.py). Permanent failures (corrupt, encrypted, unsupported)
are recorded as `failed` and not retried. Transient failures raise `TransientError` so the runner
can retry with backoff.

Cross-document rules (duplicates, PDF<->Excel verification) depend on other records, so whenever a
document is (re)validated its siblings sharing an invoice number or file hash are re-validated too.
That way the flag appears on both documents regardless of which one was processed first.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from . import db, search
from .extraction import FIELDS, ExtractionResult, extract_invoice
from .pipeline.errors import DocumentError
from .pipeline.ingest import process_file
from .validation import Issue, needs_review, normalise_number, validate

log = logging.getLogger(__name__)
EDITABLE_FIELDS = set(FIELDS) | {"line_items"}


# Serialises the cross-document step (persist -> validate -> re-validate siblings). OCR and extraction run
# in parallel across workers; only this short critical section is exclusive. Without it, two duplicate
# documents processed concurrently could each validate before the other is visible, and a stale
# `completed` status could overwrite a `needs_review` flag set by the sibling.
_CROSS_DOC_LOCK = threading.RLock()


class TransientError(Exception):
    """Retryable failure (e.g. OCR engine temporarily unavailable, unexpected crash)."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- persistence helpers

def set_status(doc_id: str, status: str, error: str | None = None, error_code: str | None = None, **extra: Any) -> None:
    cols = {"status": status, "error": error, "error_code": error_code, "updated_at": db.now_iso(), **extra}
    assignments = ", ".join(f"{k} = ?" for k in cols)
    with db.tx() as conn:
        conn.execute(f"UPDATE documents SET {assignments} WHERE id = ?", (*cols.values(), doc_id))


def record_event(doc_id: str, stage: str, status: str, message: str | None = None, duration_ms: int | None = None) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO processing_events(document_id, stage, status, message, duration_ms, created_at) VALUES (?,?,?,?,?,?)",
            (doc_id, stage, status, message, duration_ms, db.now_iso()),
        )


def mark_job(doc_id: str, status: str, last_error: str | None) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE document_id = ?",
                     (status, last_error, db.now_iso(), doc_id))


def _persist_extraction(doc_id: str, result: ExtractionResult, corrected: list[str] | None = None) -> None:
    f = result.fields
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO invoices(document_id, invoice_number, invoice_number_norm, invoice_date, supplier_name,
                 customer_name, subtotal, tax, total, currency, line_items_json, field_confidence_json,
                 raw_values_json, extraction_method, corrected_fields_json, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET
                 invoice_number=excluded.invoice_number, invoice_number_norm=excluded.invoice_number_norm,
                 invoice_date=excluded.invoice_date, supplier_name=excluded.supplier_name,
                 customer_name=excluded.customer_name, subtotal=excluded.subtotal, tax=excluded.tax,
                 total=excluded.total, currency=excluded.currency, line_items_json=excluded.line_items_json,
                 field_confidence_json=excluded.field_confidence_json, raw_values_json=excluded.raw_values_json,
                 extraction_method=excluded.extraction_method, corrected_fields_json=excluded.corrected_fields_json,
                 updated_at=excluded.updated_at""",
            (doc_id, f.get("invoice_number"), normalise_number(f.get("invoice_number")) or None,
             f.get("invoice_date"), f.get("supplier_name"), f.get("customer_name"), f.get("subtotal"),
             f.get("tax"), f.get("total"), f.get("currency"), json.dumps(result.line_items),
             json.dumps(result.confidence), json.dumps(result.raw_values), result.method,
             json.dumps(corrected or []), db.now_iso()),
        )


def _persist_issues(doc_id: str, issues: list[Issue]) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM validation_issues WHERE document_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO validation_issues(document_id, rule, severity, field, message, created_at) VALUES (?,?,?,?,?,?)",
            [(doc_id, i.rule, i.severity, i.field, i.message, db.now_iso()) for i in issues],
        )


def _other_documents(doc_id: str, invoice_number: str | None) -> list[dict[str, Any]]:
    key = normalise_number(invoice_number)
    if not key:
        return []
    return db.query_all(
        """SELECT i.document_id AS id, i.invoice_number, i.supplier_name, i.total, i.invoice_date, d.file_type
           FROM invoices i JOIN documents d ON d.id = i.document_id
           WHERE i.document_id != ? AND i.invoice_number_norm = ?
             AND d.status IN ('completed', 'needs_review')""",
        (doc_id, key),
    )


def _sha_duplicates(doc_id: str, sha: str) -> list[str]:
    return [r["id"] for r in db.query_all("SELECT id FROM documents WHERE sha256 = ? AND id != ?", (sha, doc_id))]


# --------------------------------------------------------------------------- validation

def revalidate(doc_id: str) -> tuple[str, list[Issue]]:
    """Re-run validation on the stored record, persist issues and status. No OCR or extraction."""
    inv = db.query_one("SELECT * FROM invoices WHERE document_id = ?", (doc_id,))
    doc = db.query_one("SELECT sha256, file_type FROM documents WHERE id = ?", (doc_id,))
    if inv is None or doc is None:
        raise LookupError("no extraction result for this document")
    text_row = db.query_one("SELECT warnings_json FROM extracted_text WHERE document_id = ?", (doc_id,)) or {}
    fields = {k: inv.get(k) for k in FIELDS}
    issues = validate(
        fields=fields,
        line_items=db.loads(inv.get("line_items_json"), []),
        raw_values=db.loads(inv.get("raw_values_json"), {}),
        field_confidence=db.loads(inv.get("field_confidence_json"), {}),
        processing_warnings=db.loads(text_row.get("warnings_json"), []),
        other_documents=_other_documents(doc_id, fields.get("invoice_number")),
        sha256_duplicates=_sha_duplicates(doc_id, doc["sha256"]),
        today=date.today(),
        file_type=doc["file_type"],
    )
    _persist_issues(doc_id, issues)
    status = "needs_review" if needs_review(issues) else "completed"
    set_status(doc_id, status)
    return status, issues


def _revalidate_siblings(doc_id: str, invoice_number: str | None, sha256: str) -> None:
    """Flag/unflag other documents that share an invoice number or identical file content."""
    siblings = {r["id"] for r in _other_documents(doc_id, invoice_number)}
    siblings |= set(_sha_duplicates(doc_id, sha256))
    for sib in siblings:
        if db.query_one("SELECT 1 AS ok FROM invoices WHERE document_id = ?", (sib,)):
            revalidate(sib)
            record_event(sib, "validate", "info", f"re-validated after related document {doc_id[:8]} was processed")


# --------------------------------------------------------------------------- pipeline

def process_document(doc_id: str) -> None:
    """Run the full pipeline for one document. Raises TransientError for retryable failures."""
    doc = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if doc is None:
        return
    set_status(doc_id, "processing")
    started = time.perf_counter()

    try:
        content = process_file(Path(doc["storage_path"]), doc["filename"],
                               lambda s, st, m, ms: record_event(doc_id, s, st, m, ms))
    except DocumentError as exc:
        if not exc.permanent:
            raise TransientError(exc.message) from exc
        record_event(doc_id, "failed", "error", f"{exc.code}: {exc.message}")
        set_status(doc_id, "failed", error=exc.message, error_code=exc.code)
        mark_job(doc_id, "failed", exc.message)
        search.remove_document(doc_id)
        return
    except Exception as exc:  # unexpected: retry before giving up
        raise TransientError(f"unexpected {exc.__class__.__name__}: {exc}") from exc

    with db.tx() as conn:
        conn.execute(
            """INSERT INTO extracted_text(document_id, text, ocr_confidence, warnings_json) VALUES (?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET text=excluded.text, ocr_confidence=excluded.ocr_confidence,
               warnings_json=excluded.warnings_json""",
            (doc_id, content.text, content.ocr_confidence, json.dumps(content.warnings)),
        )

    t_extract = time.perf_counter()
    try:
        result = extract_invoice(content.text, content.tables, ocr=content.ocr_confidence is not None)
    except Exception as exc:
        raise TransientError(f"extraction failed: {exc}") from exc
    result.warnings = list(dict.fromkeys(result.warnings + content.warnings))
    record_event(doc_id, "extract", "ok", f"method={result.method}", int((time.perf_counter() - t_extract) * 1000))

    with _CROSS_DOC_LOCK:
        _persist_extraction(doc_id, result)
        set_status(doc_id, "processing", error=None, error_code=None,
                   page_count=content.page_count, source_method=content.source_method)
        status, issues = revalidate(doc_id)
        search.index_document(doc_id)
        _revalidate_siblings(doc_id, result.fields.get("invoice_number"), doc["sha256"])
    record_event(doc_id, "validate", "ok", f"{len(issues)} issue(s), status={status}")
    mark_job(doc_id, "done", None)
    log.info("processed %s in %.2fs -> %s", doc_id, time.perf_counter() - started, status)


def apply_corrections(doc_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a human correction, then re-run validation on the corrected record (no re-OCR)."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Fields not editable: {sorted(unknown)}")
    inv = db.query_one("SELECT * FROM invoices WHERE document_id = ?", (doc_id,))
    if inv is None:
        raise LookupError("no extraction result for this document")

    fields = {k: inv.get(k) for k in FIELDS}
    confidence = db.loads(inv.get("field_confidence_json"), {})
    raw_values = db.loads(inv.get("raw_values_json"), {})
    corrected = set(db.loads(inv.get("corrected_fields_json"), []))
    line_items = db.loads(inv.get("line_items_json"), [])
    for name, value in changes.items():
        if name == "line_items":
            line_items = value
        else:
            fields[name] = value
        confidence[name] = 1.0  # a human has confirmed this value
        raw_values.pop(name, None)
        corrected.add(name)

    result = ExtractionResult(fields=fields, line_items=line_items, confidence=confidence,
                              raw_values=raw_values, method="manual")
    doc = db.query_one("SELECT sha256 FROM documents WHERE id = ?", (doc_id,))
    with _CROSS_DOC_LOCK:
        _persist_extraction(doc_id, result, corrected=sorted(corrected))
        status, issues = revalidate(doc_id)
        search.index_document(doc_id)
        _revalidate_siblings(doc_id, fields.get("invoice_number"), doc["sha256"])
    record_event(doc_id, "correction", "info", f"fields: {sorted(changes)}; status={status}")
    return {"status": status, "issues": [i.as_dict() for i in issues]}
