# Architecture and design decisions

## 1. System overview

```mermaid
flowchart LR
    subgraph Client
        UI[React UI<br/>upload · progress · review · search]
    end
    subgraph API[FastAPI service]
        R[REST routes<br/>/api/documents, /search, /jobs, /stats]
        AUTH[API-key check<br/>+ security headers]
        UP[Streaming upload<br/>size cap · sha256 · name sanitising]
    end
    subgraph Workers[Background job runner]
        Q[(Queue)]
        W1[Worker]
        W2[Worker]
    end
    subgraph Pipeline
        ING[Type sniffing<br/>magic bytes]
        PDF[PDF: text layer<br/>+ per-page OCR]
        IMG[Image / TIFF: preprocess<br/>+ Tesseract]
        SHT[XLSX / XLS / CSV<br/>multi-sheet, ragged rows]
        EXT[Extraction<br/>rules, optional grounded LLM]
        VAL[Validation rules<br/>+ cross-document checks]
    end
    subgraph Storage
        DB[(SQLite<br/>documents · invoices · issues · events · FTS5 · vectors)]
        FS[(Original files)]
    end
    UI -->|HTTPS JSON| R
    R --> AUTH --> UP
    UP --> FS
    UP --> DB
    UP -->|enqueue| Q
    Q --> W1 & W2
    W1 & W2 --> ING --> PDF & IMG & SHT --> EXT --> VAL
    VAL --> DB
    R --> DB
    W1 & W2 -. read original .-> FS
    EXT -. optional .-> LLM[(LLM API)]
```

## 2. Processing lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: upload accepted (202)
    queued --> processing: worker picks job
    processing --> needs_review: extracted, ≥1 error issue
    processing --> completed: extracted, no error issues
    processing --> failed: permanent error (corrupt, encrypted, type mismatch)
    processing --> queued: transient error, retry with backoff
    processing --> failed: attempts exhausted
    needs_review --> completed: manual correction clears all errors
    completed --> needs_review: a later duplicate / cross-document conflict
    failed --> queued: POST /reprocess
    needs_review --> queued: POST /reprocess
```

Every transition writes a row to `processing_events`, so the UI's "Processing history" is the audit trail.

## 3. Pipeline detail

| Input | Path | Notes |
|---|---|---|
| Digital PDF | pdfplumber text layer | Per page; pages with < 20 characters are treated as scans |
| Scanned PDF / mixed PDF | pypdfium2 render at 300 dpi → OCR for those pages only | Hybrid PDFs cost OCR only for the scanned pages |
| PNG / JPG / TIFF | grayscale → upscale to ≥1600 px → autocontrast → median denoise → Otsu threshold → Tesseract (`--psm 3`) | Multi-frame TIFF: each frame OCR'd in turn (up to 20) |
| XLSX / XLS | every sheet read raw (`header=None`), empty rows/columns removed | Produces both a cell grid (for line-item tables) and `Label: value` text |
| CSV | csv module with delimiter sniffing, encoding fallback (utf-8-sig → cp1252 → latin-1), rows padded | Key/value header block plus item table in one file |

Failure handling by class:

| Class | Examples | Behaviour |
|---|---|---|
| Permanent | corrupt bytes, truncated PDF, password-protected PDF, extension ≠ content, empty document | `failed` immediately with `error_code`; never retried |
| Transient | Tesseract not installed or crashed, unexpected exception | retried with exponential backoff (1 s, 2 s, …) up to `DI_MAX_ATTEMPTS`, then `failed` with `processing_failed` |
| Resource limits | file > 25 MB, > 50 files per request, > 200 OCR pages, > 20 sheets, > 20 000 rows per sheet | rejected or truncated with an explicit warning |

## 4. Extraction

Two extractors share one output contract (`ExtractionResult`: fields, line items, per-field confidence,
raw unparseable values, warnings).

**Rule-based (default, deterministic).**
* Labels are matched per line, with synonyms (`Invoice No`, `Inv. #`, `Bill No`, …; `Vendor`, `Seller`, `From`, …).
  A label may have its value on the same line or the next non-empty line (`Bill To:` blocks).
* Amounts: handles `1,234.50`, `1.234,50`, currency symbols and codes, and rate notes such as `VAT (15%)`.
* Dates: ISO, `dd/mm/yyyy`, `mm/dd/yyyy`, `15 Mar 2025`, `March 15, 2025`, `15-Mar-2025`. Ambiguous
  day/month pairs are read day-first and marked lower confidence with a warning.
* Currency: ISO code near the total first, then `Tk`/`BDT`, then symbols (`$` is low confidence because
  several currencies use it).
* Line items: from the table (header detection on spreadsheets) or from `desc qty unit amount` text lines.

**Optional LLM (`ANTHROPIC_API_KEY`).** The model returns JSON. Every value is then **grounded**: a value is
kept only if its literal form appears in the document text. Ungrounded values are discarded with a
`llm_value_discarded:<field>` warning and the rule-based value is kept. Any HTTP, timeout or JSON error
falls back to the rules with a warning. Prompt: see [AI_USAGE.md](AI_USAGE.md).

**Never invent.** Missing → `null`, confidence absent. Validation then reports `MISSING_*`.

## 5. Validation

Validation is a pure function of (record, line items, other documents, processing warnings). That makes
each rule unit-testable and keeps the database out of the rule logic. Cross-document rules need the other
records, so the orchestrator passes in same-invoice-number documents and same-hash files.

Cross-document decision table (same normalised invoice number, other document found):

| Supplier | Values | File types | Result |
|---|---|---|---|
| same | differ | any | `CROSS_DOCUMENT_MISMATCH` (error) |
| same | agree | same | `DUPLICATE_INVOICE` (error) |
| same | agree | different | `CROSS_DOCUMENT_MATCH` (warning, "verified") |
| different | any | any | `INVOICE_NUMBER_REUSED` (warning) |

When a document is processed or corrected, its siblings (same number or same file hash) are re-validated,
so a flag appears on both members of a pair whichever is processed first.

## 6. Concurrency

* Workers run OCR and extraction in parallel.
* The cross-document step (persist → validate → re-validate siblings) runs under one process-wide lock.
  Without it, two duplicates processed on parallel workers could each validate before the other is
  visible, and a stale `completed` could overwrite a `needs_review` flag. The evaluation caught this
  (see EVALUATION.md, "Defects found during evaluation").
* SQLite runs in WAL mode with a busy timeout, and each write is a short transaction.

## 7. Database schema

Tables (full DDL in `backend/app/db.py`):

```mermaid
erDiagram
    documents ||--o| extracted_text : "has"
    documents ||--o| invoices : "has"
    documents ||--o{ validation_issues : "has"
    documents ||--o{ processing_events : "has"
    documents ||--o| jobs : "has"
    documents ||--o| embeddings : "has"
    documents {
        text id PK
        text filename
        text file_type
        int size_bytes
        text sha256 "indexed"
        text storage_path
        text status "queued|processing|completed|needs_review|failed"
        text error_code
        int page_count
        text source_method
        text created_at
        text updated_at
    }
    extracted_text {
        text document_id PK, FK
        text text
        real ocr_confidence
        text warnings_json
    }
    invoices {
        text document_id PK, FK
        text invoice_number
        text invoice_number_norm "indexed, duplicate lookups"
        text invoice_date "ISO 8601"
        text supplier_name
        text customer_name
        real subtotal
        real tax
        real total
        text currency "ISO 4217"
        text line_items_json
        text field_confidence_json
        text raw_values_json
        text extraction_method "rules|llm|manual"
        text corrected_fields_json
    }
    validation_issues {
        int id PK
        text document_id FK
        text rule
        text severity "error|warning"
        text field
        text message
    }
    processing_events {
        int id PK
        text document_id FK
        text stage
        text status
        text message
        int duration_ms
        text created_at
    }
    jobs {
        int id PK
        text document_id FK "unique"
        text status
        int attempts
        text last_error
    }
    embeddings {
        text document_id PK, FK
        text vector_json "sparse {index: weight}"
    }
```

`documents_fts` is an FTS5 virtual table (`porter unicode61` tokenizer) holding filename and body text
(extracted text plus the invoice identifiers) for keyword search.

Production mapping: the same schema translates to PostgreSQL with `jsonb` for the JSON columns, a
`tsvector` GIN index in place of FTS5, `pgvector` for embeddings, and `invoice_number_norm` as a btree index.

## 8. Search

| Mode | Mechanism | Strength | Weakness |
|---|---|---|---|
| keyword | FTS5 `MATCH` with quoted terms, BM25 ranking, `snippet()` highlights | exact identifiers (`INV-2025-0042`), names | no synonyms |
| semantic (vector) | stateless `HashingVectorizer` (1–2 grams, English stop-words, 2¹⁸ buckets), L2-normalised, cosine via sparse dot product | finds related documents with overlapping vocabulary; no re-fitting when documents arrive | not neural; no synonym or paraphrase understanding |
| hybrid | reciprocal-rank fusion, k = 60, over both rankings | robust default | — |

For production, the vector store would hold dense sentence embeddings (for example from the LLM provider or a
local model) in `pgvector`, with an approximate index such as HNSW. Only `search.py` changes.

## 9. Scalability and reliability

| Concern | Current | Scale-out path |
|---|---|---|
| Ingest throughput | Streaming upload, bounded worker pool | Separate API and worker processes; object storage for files |
| Queue | In-process threads plus DB-backed job rows; re-queue on restart | BullMQ/Redis or SQS with the same job contract; `submit()` and the worker loop change only |
| OCR cost | Digital text first; OCR only scanned pages; per-PDF page cap | Dedicated OCR worker pool, GPU OCR for large volume |
| Database | SQLite WAL | PostgreSQL; read replicas for search and listing |
| Duplicate checks | Indexed `invoice_number_norm` lookup | Same index in Postgres; unique constraint per (supplier, number) |
| Failure isolation | Permanent vs transient classes; worker loop never dies on an exception | Dead-letter queue for jobs exhausted after retries |
| Observability | `processing_events`, `/api/stats`, `/api/jobs` | OpenTelemetry traces and metrics |

## 10. Security

* Extension allow-list **and** content sniffing (magic bytes). A `.png` that is really text is rejected.
* Filenames are reduced to a basename and sanitised; stored files use UUID names, so client filenames never
  reach the filesystem path. Downloads are checked to stay inside the upload directory.
* Size limits enforced while streaming, not after buffering.
* Optional API-key authentication (`DI_API_KEY`); liveness endpoint stays open.
* Strict Pydantic validation on corrections: unknown fields are rejected (`extra="forbid"`), currency must be a
  three-letter code, lengths are bounded.
* Security headers: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. CORS restricted to
  configured origins.
* SQL is parameterised throughout; LIKE filters escape wildcards.
* The UI renders all extracted text as React text nodes. Only the `<mark>` search highlight is interpreted.
* Not yet implemented: user accounts and per-user authorisation, rate limiting, encryption at rest, malware
  scanning of uploads. These are the next items for production.

## 11. Trade-offs

| Decision | Alternative considered | Why this choice |
|---|---|---|
| SQLite + FTS5 | PostgreSQL from day one | Runs with zero infrastructure while keeping the schema portable |
| In-process job runner | BullMQ/Redis | Same contract, no broker to run for evaluation; retry and recovery are still real |
| Rule-based extraction as default | LLM only | Deterministic, free, testable; the LLM is an upgrade that is grounded, not trusted |
| Stateless hashing vectors | Dense embeddings | No model download or API dependency; swap point is isolated in `search.py` |
| FastAPI | NestJS | Python-first OCR and data stack (Tesseract, pandas, pdfplumber) |
| React + Vite | Next.js | A single-page operator tool does not need SSR; smaller build |
