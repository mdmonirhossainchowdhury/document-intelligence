"""REST API for the document intelligence service.

Endpoints (all under /api):
  POST   /documents                       upload one or more files (async processing, returns 202)
  GET    /documents                       list with keyword filters (status, supplier, currency, ...)
  GET    /documents/{id}                  document + extraction + issues + processing history
  GET    /documents/{id}/status           lightweight progress poll
  GET    /documents/{id}/extraction       structured invoice JSON
  PATCH  /documents/{id}/extraction       manual corrections (re-validated, audit-logged)
  GET    /documents/{id}/issues           validation issues
  GET    /documents/{id}/file             original file download
  POST   /documents/{id}/reprocess        re-run the pipeline (not while queued/processing)
  POST   /documents/reprocess-failed      re-run every failed document
  DELETE /documents/{id}                  delete document, file, index entries
  GET    /search?q=&mode=keyword|semantic|hybrid
  GET    /jobs?status=failed              job queue view
  GET    /stats                           counts, failure rate, processing-time summary
  GET    /health
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db, processing, search
from .config import SUPPORTED_EXTENSIONS, settings
from .jobs import runner

log = logging.getLogger("docintel")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ()]")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    runner.start()
    yield


app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def require_api_key(request: Request) -> None:
    if settings.api_key and request.headers.get("X-API-Key") != settings.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")


api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


# --------------------------------------------------------------------------- schemas

class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=300)
    quantity: float | None = None
    unit_price: float | None = None
    amount: float


class ExtractionPatch(BaseModel):
    """Only fields present in the request body are changed."""
    model_config = ConfigDict(extra="forbid")
    invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: date | None = None
    supplier_name: str | None = Field(default=None, max_length=200)
    customer_name: str | None = Field(default=None, max_length=200)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    currency: str | None = None
    line_items: list[LineItem] | None = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def iso_currency(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return v


# --------------------------------------------------------------------------- helpers

_SUMMARY_SQL = """
SELECT d.id, d.filename, d.file_type, d.status, d.error, d.error_code, d.page_count, d.source_method,
       d.size_bytes, d.created_at, d.updated_at,
       i.invoice_number, i.invoice_date, i.supplier_name, i.customer_name, i.total, i.currency,
       (SELECT COUNT(*) FROM validation_issues v WHERE v.document_id = d.id AND v.severity = 'error') AS error_count,
       (SELECT COUNT(*) FROM validation_issues v WHERE v.document_id = d.id AND v.severity = 'warning') AS warning_count
FROM documents d LEFT JOIN invoices i ON i.document_id = d.id
"""


def _get_document(doc_id: str) -> dict[str, Any]:
    row = db.query_one(_SUMMARY_SQL + " WHERE d.id = ?", (doc_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return row


def _extraction(doc_id: str) -> dict[str, Any]:
    inv = db.query_one("SELECT * FROM invoices WHERE document_id = ?", (doc_id,))
    if inv is None:
        return {"document_id": doc_id, "available": False}
    return {
        "document_id": doc_id,
        "available": True,
        "invoice_number": inv["invoice_number"],
        "invoice_date": inv["invoice_date"],
        "supplier_name": inv["supplier_name"],
        "customer_name": inv["customer_name"],
        "subtotal": inv["subtotal"],
        "tax": inv["tax"],
        "total": inv["total"],
        "currency": inv["currency"],
        "line_items": db.loads(inv["line_items_json"], []),
        "field_confidence": db.loads(inv["field_confidence_json"], {}),
        "extraction_method": inv["extraction_method"],
        "corrected_fields": db.loads(inv["corrected_fields_json"], []),
    }


def _issues(doc_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT id, rule, severity, field, message, created_at FROM validation_issues WHERE document_id = ? ORDER BY severity, id",
        (doc_id,),
    )


def _events(doc_id: str) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT stage, status, message, duration_ms, created_at FROM processing_events WHERE document_id = ? ORDER BY id",
        (doc_id,),
    )


def _safe_filename(name: str) -> str:
    base = Path(name.replace("\\", "/")).name  # strip any directory components
    base = _SAFE_NAME.sub("_", base).strip(" .")[:150]
    return base or "upload"


async def _store_upload(upload: UploadFile, dest: Path) -> tuple[int, str]:
    import hashlib

    digest, size = hashlib.sha256(), 0
    with dest.open("wb") as out:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413,
                                    f"file exceeds {settings.max_upload_bytes // (1024 * 1024)} MB limit")
            digest.update(chunk)
            out.write(chunk)
    return size, digest.hexdigest()


# --------------------------------------------------------------------------- routes

@app.get("/api/health", tags=["ops"])
def health() -> dict[str, Any]:
    return {"status": "ok", "queue_depth": runner.queue_depth()}


@api.post("/documents", status_code=status.HTTP_202_ACCEPTED, tags=["documents"])
async def upload_documents(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files provided")
    if len(files) > settings.max_files_per_request:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"at most {settings.max_files_per_request} files per request")

    accepted, rejected = [], []
    for upload in files:
        name = _safe_filename(upload.filename or "")
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            rejected.append({"filename": name, "reason": f"unsupported type '{ext or 'none'}'"})
            continue
        doc_id = str(uuid.uuid4())
        dest = settings.uploads_dir / f"{doc_id}{ext}"
        try:
            size, sha = await _store_upload(upload, dest)
        except HTTPException as exc:
            rejected.append({"filename": name, "reason": exc.detail})
            continue
        if size == 0:
            dest.unlink(missing_ok=True)
            rejected.append({"filename": name, "reason": "file is empty"})
            continue
        now = db.now_iso()
        duplicate_of = db.query_one("SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha,))
        with db.tx() as conn:
            conn.execute(
                """INSERT INTO documents(id, filename, file_type, mime_hint, size_bytes, sha256, storage_path,
                       status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, name, SUPPORTED_EXTENSIONS[ext], upload.content_type, size, sha, str(dest), "queued", now, now),
            )
        runner.submit(doc_id)
        accepted.append({"id": doc_id, "filename": name, "status": "queued",
                         "duplicate_of": duplicate_of["id"] if duplicate_of else None})
    return {"accepted": accepted, "rejected": rejected}


@api.get("/documents", tags=["documents"])
def list_documents(
    status_filter: Literal["queued", "processing", "completed", "needs_review", "failed"] | None = Query(None, alias="status"),
    supplier: str | None = Query(None, max_length=200),
    customer: str | None = Query(None, max_length=200),
    currency: str | None = Query(None, max_length=3),
    invoice_number: str | None = Query(None, max_length=60),
    date_from: date | None = None,
    date_to: date | None = None,
    file_type: Literal["pdf", "image", "spreadsheet"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    clauses, params = [], []
    if status_filter:
        clauses.append("d.status = ?"); params.append(status_filter)
    if supplier:
        clauses.append("i.supplier_name LIKE ? ESCAPE '\\'"); params.append(_like(supplier))
    if customer:
        clauses.append("i.customer_name LIKE ? ESCAPE '\\'"); params.append(_like(customer))
    if currency:
        clauses.append("i.currency = ?"); params.append(currency.upper())
    if invoice_number:
        clauses.append("i.invoice_number_norm LIKE ? ESCAPE '\\'"); params.append(_like(re.sub(r"[^A-Za-z0-9]", "", invoice_number).upper()))
    if date_from:
        clauses.append("i.invoice_date >= ?"); params.append(date_from.isoformat())
    if date_to:
        clauses.append("i.invoice_date <= ?"); params.append(date_to.isoformat())
    if file_type:
        clauses.append("d.file_type = ?"); params.append(file_type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = db.query_one(f"SELECT COUNT(*) AS n FROM documents d LEFT JOIN invoices i ON i.document_id = d.id{where}", tuple(params))
    rows = db.query_all(_SUMMARY_SQL + where + " ORDER BY d.created_at DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    return {"total": total["n"] if total else 0, "limit": limit, "offset": offset, "items": rows}


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@api.get("/documents/{doc_id}", tags=["documents"])
def get_document(doc_id: str) -> dict[str, Any]:
    doc = _get_document(doc_id)
    text_row = db.query_one("SELECT text, ocr_confidence, warnings_json FROM extracted_text WHERE document_id = ?", (doc_id,))
    return {
        **doc,
        "extraction": _extraction(doc_id),
        "issues": _issues(doc_id),
        "events": _events(doc_id),
        "text_preview": (text_row["text"][:4000] if text_row else None),
        "ocr_confidence": text_row["ocr_confidence"] if text_row else None,
        "warnings": db.loads(text_row["warnings_json"], []) if text_row else [],
    }


@api.get("/documents/{doc_id}/status", tags=["documents"])
def get_status(doc_id: str) -> dict[str, Any]:
    doc = _get_document(doc_id)
    job = db.query_one("SELECT attempts, last_error FROM jobs WHERE document_id = ?", (doc_id,)) or {}
    last = db.query_one("SELECT stage, message FROM processing_events WHERE document_id = ? ORDER BY id DESC LIMIT 1", (doc_id,))
    return {"id": doc_id, "status": doc["status"], "error": doc["error"], "error_code": doc["error_code"],
            "attempts": job.get("attempts", 0), "last_stage": last["stage"] if last else None,
            "last_message": last["message"] if last else None}


@api.get("/documents/{doc_id}/extraction", tags=["documents"])
def get_extraction(doc_id: str) -> dict[str, Any]:
    _get_document(doc_id)
    return _extraction(doc_id)


@api.patch("/documents/{doc_id}/extraction", tags=["documents"])
def patch_extraction(doc_id: str, body: ExtractionPatch) -> dict[str, Any]:
    _get_document(doc_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "no fields to update")
    if changes.get("invoice_date") is not None:
        changes["invoice_date"] = changes["invoice_date"].isoformat()
    if "line_items" in changes and changes["line_items"] is None:
        changes["line_items"] = []  # explicit null clears the table
    try:
        result = processing.apply_corrections(doc_id, changes)
    except LookupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"extraction": _extraction(doc_id), **result}


@api.get("/documents/{doc_id}/issues", tags=["documents"])
def get_issues(doc_id: str) -> dict[str, Any]:
    _get_document(doc_id)
    return {"items": _issues(doc_id)}


@api.get("/documents/{doc_id}/file", tags=["documents"])
def download_original(doc_id: str) -> FileResponse:
    doc = _get_document(doc_id)
    path = Path(db.query_one("SELECT storage_path FROM documents WHERE id = ?", (doc_id,))["storage_path"]).resolve()
    if settings.uploads_dir.resolve() not in path.parents:  # defence in depth against path tampering
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return FileResponse(path, filename=doc["filename"], media_type="application/octet-stream")


@api.post("/documents/{doc_id}/reprocess", status_code=status.HTTP_202_ACCEPTED, tags=["jobs"])
def reprocess(doc_id: str) -> dict[str, Any]:
    doc = _get_document(doc_id)
    if doc["status"] in ("queued", "processing"):
        raise HTTPException(status.HTTP_409_CONFLICT, "document is already queued or processing")
    runner.submit(doc_id)
    return {"id": doc_id, "status": "queued"}


@api.post("/documents/reprocess-failed", status_code=status.HTTP_202_ACCEPTED, tags=["jobs"])
def reprocess_failed() -> dict[str, Any]:
    rows = db.query_all("SELECT id FROM documents WHERE status = 'failed'")
    for row in rows:
        runner.submit(row["id"])
    return {"requeued": len(rows)}


@api.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["documents"])
def delete_document(doc_id: str) -> Response:
    doc = _get_document(doc_id)
    if doc["status"] in ("queued", "processing"):
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot delete while processing")
    path = Path(db.query_one("SELECT storage_path FROM documents WHERE id = ?", (doc_id,))["storage_path"])
    search.remove_document(doc_id)
    with db.tx() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    if settings.uploads_dir.resolve() in path.resolve().parents:
        path.unlink(missing_ok=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.get("/search", tags=["search"])
def search_documents(
    q: str = Query(..., min_length=1, max_length=300),
    mode: Literal["keyword", "semantic", "hybrid"] = "hybrid",
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    hits = search.search(q, mode, limit)
    if not hits:
        return {"query": q, "mode": mode, "items": []}
    ids = [h["document_id"] for h in hits]
    placeholders = ",".join("?" for _ in ids)
    rows = {r["id"]: r for r in db.query_all(_SUMMARY_SQL + f" WHERE d.id IN ({placeholders})", tuple(ids))}
    items = []
    for h in hits:
        if h["document_id"] in rows:
            items.append({**rows[h["document_id"]], "score": h["score"], "snippet": h.get("snippet")})
    return {"query": q, "mode": mode, "items": items}


@api.get("/jobs", tags=["jobs"])
def list_jobs(status_filter: Literal["queued", "running", "done", "failed"] | None = Query(None, alias="status"),
              limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    if status_filter:
        rows = db.query_all("SELECT j.*, d.filename FROM jobs j JOIN documents d ON d.id = j.document_id WHERE j.status = ? ORDER BY j.updated_at DESC LIMIT ?", (status_filter, limit))
    else:
        rows = db.query_all("SELECT j.*, d.filename FROM jobs j JOIN documents d ON d.id = j.document_id ORDER BY j.updated_at DESC LIMIT ?", (limit,))
    return {"items": rows, "queue_depth": runner.queue_depth()}


@api.get("/stats", tags=["ops"])
def stats() -> dict[str, Any]:
    by_status = {r["status"]: r["n"] for r in db.query_all("SELECT status, COUNT(*) AS n FROM documents GROUP BY status")}
    total = sum(by_status.values())
    processed = by_status.get("completed", 0) + by_status.get("needs_review", 0) + by_status.get("failed", 0)
    timing = db.query_one("SELECT AVG(duration_ms) AS avg_ms, MAX(duration_ms) AS max_ms FROM processing_events WHERE stage = 'parse'") or {}
    return {
        "total_documents": total,
        "by_status": by_status,
        "failure_rate": round(by_status.get("failed", 0) / processed, 4) if processed else 0.0,
        "avg_parse_ms": round(timing.get("avg_ms") or 0, 1),
        "queue_depth": runner.queue_depth(),
    }


app.include_router(api)
