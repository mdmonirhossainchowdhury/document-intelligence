# AI-Powered Document Intelligence System

Processes invoices and financial documents (PDF, scanned PDF, JPG/PNG/TIFF, XLSX/XLS, CSV):
extracts structured invoice data, validates it, flags records that need a human, makes everything
searchable, and gives reviewers a web UI to correct values and reprocess failures.

| Brief task | Where it lives |
|---|---|
| 1. Document processing | `backend/app/pipeline/` (PDF text + OCR fallback, image OCR with preprocessing, multi-sheet XLSX/XLS, CSV with ragged rows, corrupt-file handling) |
| 2. AI information extraction | `backend/app/extraction.py` (rule engine + optional grounded LLM) |
| 3. Data validation | `backend/app/validation.py` (missing fields, invalid numbers, arithmetic, duplicates, PDF↔Excel cross-checks, review flags) |
| 4. Backend API | `backend/app/main.py`, background jobs in `backend/app/jobs.py` |
| 5. Database and search | `backend/app/db.py` (SQLite + FTS5), `backend/app/search.py` (keyword, similarity, hybrid) |
| 6. Frontend | `frontend/` (Vite + React) |
| 7. Testing and evaluation | `tests/` (88 tests, 91% backend line coverage), `eval/` (labelled dataset and harness), `docs/EVALUATION.md` |

Supporting docs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagram, schema, design decisions),
[docs/EVALUATION.md](docs/EVALUATION.md) (measured results and limitations),
[docs/AI_USAGE.md](docs/AI_USAGE.md) (AI-assisted development disclosure and prompts).

---

## Quick start

Requirements: Python 3.11+ (tested on 3.13), Node 18+, and the **Tesseract OCR** binary for scanned input
(`apt install tesseract-ocr` or `brew install tesseract`). Digital PDFs, spreadsheets and CSV work without it.

```bash
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000          # API docs: http://localhost:8000/docs

# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev                                # UI: http://localhost:5173 (proxies /api to :8000)
```

Production-style build of the UI: `npm run build` then `npm run preview`.

If the API is started with `DI_API_KEY`, set the same value in the browser console once:
`localStorage.setItem("di_api_key", "<key>")`. This build has no login screen.

Settings are read from the process environment. `.env.example` lists them; export them or load with
`set -a; source .env; set +a` before starting uvicorn.

### Run the tests

```bash
cd backend && pip install -r requirements-dev.txt && cd ..   # from the project root
python -m pytest tests -q                   # 88 tests, ~10 s
python -m pytest tests --cov=backend/app --cov-report=term-missing   # coverage
```

Tests generate their own fixtures (PDFs, scans, spreadsheets, corrupt files), so nothing needs to be
checked in. OCR tests are skipped automatically if Tesseract is not installed.

### Run the evaluation

```bash
python eval/run_eval.py                     # regenerates eval/dataset (72 labelled files) and evaluates
                                            # needs backend/requirements-dev.txt (reportlab, rapidfuzz)
```

Writes `eval/results/metrics.json`, `eval/results/REPORT.md` and `eval/results/per_document.csv`.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DI_DATA_DIR` | `./data` | Uploaded files and the SQLite database |
| `DI_API_KEY` | unset | If set, every `/api` call must send `X-API-Key` |
| `ANTHROPIC_API_KEY` | unset | If set, LLM extraction is enabled (grounded; falls back to rules on any error) |
| `DI_LLM_MODEL` | `claude-haiku-5-5` | Model used for LLM extraction |
| `DI_WORKERS` | `2` | Background worker threads |
| `DI_MAX_ATTEMPTS` | `2` | Attempts per job for transient failures |
| `DI_MAX_UPLOAD_MB` | `25` | Per-file size limit |
| `DI_MAX_FILES` | `50` | Files per upload request |
| `DI_MAX_OCR_PAGES` | `200` | OCR page cap per PDF (cost control) |
| `DI_CORS_ORIGINS` | `http://localhost:5173` | Allowed browser origins |

---

## API

All endpoints are under `/api`. Interactive docs at `/docs`.

| Method | Path | Description |
|---|---|---|
| POST | `/documents` | Upload one or more files (multipart field `files`). Returns `202` with `accepted` and `rejected`. |
| GET | `/documents` | List with filters: `status`, `supplier`, `customer`, `currency`, `invoice_number`, `date_from`, `date_to`, `file_type`, `limit`, `offset` |
| GET | `/documents/{id}` | Document, extraction, issues, processing history, text preview |
| GET | `/documents/{id}/status` | Lightweight progress poll (status, attempts, last stage) |
| GET | `/documents/{id}/extraction` | Structured invoice JSON with per-field confidence |
| PATCH | `/documents/{id}/extraction` | Manual correction of any subset of fields or `line_items`; re-validates immediately |
| GET | `/documents/{id}/issues` | Validation issues (rule, severity, field, message) |
| GET | `/documents/{id}/file` | Download the original file |
| POST | `/documents/{id}/reprocess` | Re-run the pipeline (`409` if already queued or processing) |
| POST | `/documents/reprocess-failed` | Re-queue every failed document |
| DELETE | `/documents/{id}` | Delete document, file and search index entries |
| GET | `/search?q=&mode=keyword\|semantic\|hybrid` | Search; `semantic` is vector similarity (see limitations) |
| GET | `/jobs?status=` | Job queue view |
| GET | `/stats` | Counts by status, failure rate, average parse time |
| GET | `/health` | Liveness (always open, even when an API key is configured) |

Example:

```bash
curl -F "files=@invoice.pdf" -F "files=@sheet.xlsx" http://localhost:8000/api/documents
curl "http://localhost:8000/api/documents?status=needs_review"
curl -X PATCH -H "Content-Type: application/json" -d '{"total": 330.00}' http://localhost:8000/api/documents/<id>/extraction
```

### Extracted record (example)

```json
{
  "invoice_number": "INV-2025-0042",
  "invoice_date": "2025-03-15",
  "supplier_name": "Apex Office Supplies Ltd",
  "customer_name": null,
  "subtotal": 2800.0,
  "tax": 280.0,
  "total": 3080.0,
  "currency": "USD",
  "line_items": [{"description": "Laptop stand", "quantity": 2, "unit_price": 1250.0, "amount": 2500.0}],
  "field_confidence": {"invoice_number": 0.9, "total": 0.9},
  "extraction_method": "rules",
  "corrected_fields": []
}
```

`customer_name` is `null` because the document does not contain one. The system never guesses a value
it cannot find; the validation layer reports the gap instead.

### Validation rules

| Rule | Severity | Blocks review? |
|---|---|---|
| `MISSING_REQUIRED_FIELD` (invoice number, date, supplier, total) | error | yes |
| `MISSING_FIELD` (customer, currency, subtotal, tax) | warning | no |
| `INVALID_NUMBER` (e.g. total = "TBD") | error | yes |
| `NEGATIVE_AMOUNT`, `INVALID_DATE` | error | yes |
| `TOTAL_MISMATCH` (subtotal + tax ≠ total) | error | yes |
| `LINE_ITEMS_SUBTOTAL_MISMATCH` (items don't sum to subtotal) | error | yes |
| `LINE_ITEM_ARITHMETIC` (qty × unit ≠ amount) | warning | no |
| `DUPLICATE_INVOICE` (same number + supplier + file type) | error | yes |
| `CROSS_DOCUMENT_MISMATCH` (same supplier and number, different values) | error | yes |
| `CROSS_DOCUMENT_MATCH` (PDF and Excel agree) | warning (verified) | no |
| `INVOICE_NUMBER_REUSED` (same number, different supplier) | warning | no |
| `DUPLICATE_FILE` (identical bytes already uploaded) | warning | no |
| `POOR_IMAGE_QUALITY`, `LOW_FIELD_CONFIDENCE`, `FUTURE_DATE`, `UNKNOWN_CURRENCY`, … | warning / error | see code |

A document is `needs_review` when any error exists, otherwise `completed`. Corrupt, unsupported,
mismatched and encrypted files end as `failed` with an `error_code` and are not retried.

---

## Project layout

```
backend/app/
  main.py          REST API, auth, upload handling, security headers
  config.py        settings from environment
  db.py            SQLite schema (portable SQL) and helpers
  jobs.py          background worker pool, retries with backoff, crash recovery
  processing.py    per-document pipeline orchestration, corrections, cross-document re-validation
  extraction.py    rule-based and grounded LLM extraction, parsers for amounts and dates
  validation.py    pure validation rules (unit-testable)
  search.py        FTS5 keyword search, vector similarity, reciprocal-rank fusion
  pipeline/        ingest (type sniffing), pdf_proc, image_proc, sheet_proc
frontend/src/      App, Upload (live progress), DocumentList, DocumentDetail (review + edit), Search
tests/             unit, pipeline, and API tests (pytest)
eval/              dataset generator, evaluation harness, results
docs/              architecture, evaluation report, AI usage disclosure
```

## Limitations (summary; details in docs)

* The evaluation dataset is **synthetic**, rendered from templates. The numbers show behaviour on those
  templates and on degraded scans; they are not a substitute for measuring on real customer invoices.
* The LLM extraction path is implemented and unit-tested with a mocked HTTP transport, but was not run
  against the live API in this build (no key available). It is off unless `ANTHROPIC_API_KEY` is set.
* "Similarity" search is TF-IDF-style vector matching, not neural embeddings.
* Job queue and database are embedded (threads + SQLite) for zero-setup use. The production path
  (BullMQ/Redis, PostgreSQL + pgvector) is described in `docs/ARCHITECTURE.md`.
