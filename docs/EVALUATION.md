# Evaluation report

This report contains only numbers produced by `eval/run_eval.py` (results in `eval/results/`). Re-run the
command to reproduce them. The dataset is generated with a fixed seed.

## 1. What was measured, and on what

The harness uploads every sample through the real `POST /api/documents` endpoint in batches of 25, waits for
the background workers, re-reads every final status once the batch has settled, and scores the stored
results against the ground truth in `eval/dataset/labels.json`.

**The dataset is synthetic.** It is rendered from invoice templates written for this project, with several
label styles, date formats, currency notations and number formats. It contains real PDF, PNG, XLSX and CSV
files, including degraded scans and deliberately broken files. It is **not** a sample of real customer
invoices. Treat these numbers as a measurement of the system's behaviour on this variety of document, not as
production accuracy.

| Category | Samples | What it exercises |
|---|---|---|
| digital_pdf | 20 | Text-layer PDFs, 1–2 pages, some with optional fields missing |
| scanned_pdf | 6 | Raster-only PDF pages, OCR path |
| image_clean | 6 | PNG renders of invoices, clean |
| image_degraded | 6 | Same, after blur, speckle noise and 0.8° skew |
| xlsx | 10 | Two sheets (summary + line items) |
| csv | 8 | Key/value header block followed by an item table |
| cross_verify | 6 | Same invoices as PDF and as XLSX (values agree) |
| arithmetic_error | 3 | Printed total ≠ subtotal + tax |
| duplicate | 2 | Two PDFs with the same number and supplier |
| missing_required | 1 | Supplier not present |
| corrupt | 4 | Random bytes, truncated PDF, text named `.png`, non-zip `.xlsx` |

## 2. Headline results

| Metric | Result |
|---|---|
| Document success rate (invoices that reached completed or needs_review) | 100.0% (68 invoices) |
| Document failure rate (invoices ending `failed`) | 0.0% |
| Corrupt or unreadable files correctly failed | 4/4 |
| Overall field accuracy (8 fields; a correctly absent field counts as correct) | 96.0% |
| Invented values (a value populated where the truth is absent) | 0 of 19 absent-field cases (0.0%) |
| Line-item list exact match (all rows and amounts, in order) | 98.5% |
| Median processing time per document (parse + extract, excl. queue) | 11.0 ms |
| 95th-percentile processing time per document | 998 ms (scanned PDFs and images dominate) |
| Batch wall-clock for all 72 files, 2 workers | 8.57 s (8.4 docs/s) |

Corrupt files were rejected with these codes: `corrupt_file` × 3, `type_mismatch` × 1.

### Field-level accuracy

| Field | Accuracy | Recall on fields that exist | Invented values |
|---|---|---|---|
| invoice_number | 92.7% | 94.1% | 0 |
| invoice_date | 97.1% | 97.1% | 0 |
| supplier_name | 97.1% | 98.5% | 0 |
| customer_name | 97.1% | 98.4% | 0 |
| subtotal | 91.2% | 91.2% | 0 |
| tax | 100.0% | 100.0% | 0 |
| total | 95.6% | 95.6% | 0 |
| currency | 97.1% | 97.1% | 0 |

Accuracy rules: invoice number compared alphanumerically, case-insensitive; dates exact; supplier and
customer names by token-sort similarity ≥ 90; amounts within 0.01; currency exact.

### Per category

| Category | Samples | Processed | Failed | Flagged for review | Field accuracy |
|---|---|---|---|---|---|
| digital_pdf | 20 | 20 | 0 | 0 | 100.0% |
| scanned_pdf | 6 | 6 | 0 | 0 | 100.0% |
| image_clean | 6 | 6 | 0 | 0 | 100.0% |
| image_degraded | 6 | 6 | 0 | 6 | 54.2% |
| xlsx | 10 | 10 | 0 | 0 | 100.0% |
| csv | 8 | 8 | 0 | 0 | 100.0% |
| cross_verify | 6 | 6 | 0 | 0 | 100.0% |
| arithmetic_error | 3 | 3 | 0 | 3 | 100.0% |
| duplicate | 2 | 2 | 0 | 2 | 100.0% |
| missing_required | 1 | 1 | 0 | 1 | 100.0% |
| corrupt | 4 | 0 | 4 | 0 | n/a |

Every category outside `image_degraded` is at 100% field accuracy. That is expected for synthetic templates
the rules were written against, and it is the reason the synthetic numbers above should not be read as
production accuracy. The degraded-scan group is the honest signal: OCR errors such as `Subtotnt` for
`Subtotal`, `Toted Dus` for `Total Due`, and digit confusions in invoice numbers (`INV-2025-7861` read for
`INV-2025-7881`) reduce accuracy to 54.2%. Tesseract still reported 74–87 mean word confidence
on those pages, so the confidence score alone would not have caught them. What catches them is the
required-field and arithmetic rules, and the record is flagged rather than silently accepted.

## 3. Review flagging (manual-review queue)

A "flag" is a record with status `needs_review`. Three references are reported, because they answer
different questions:

| Reference | Question it answers | Precision | Recall | TP / FP / FN / TN |
|---|---|---|---|---|
| Design labels (`labels.json`) | Did the document contain the defect the fixture was built with? | 50.0% | 100.0% | 6 / 6 / 0 / 56 |
| Measured extraction errors | Did the stored record disagree with the ground truth? | 50.0% | 100.0% | 6 / 6 / 0 / 56 |
| Combined: defect OR extraction error | Should a human have looked at this record? | 100.0% | 100.0% | 12 / 0 / 0 / 56 |

Reading the table:

* **Recall is 100% on every reference.** No record that needed a human was passed through as clean.
* **The 50% precision on the first two rows comes from the six degraded scans.** Their labels say "clean"
  because the fixture was meant to test OCR, not review. Every one of them has wrong fields in the stored
  record, so flagging them is the correct result. The combined row, which counts those records as
  needing review, gives 100% precision.
* The labels were written before the run. The combined definition was added after the first run, once the
  labels turned out to undercount extraction errors. Both are reported so the change is visible.

## 4. Defects found during evaluation and fixed

These were found by running the harness, not by inspection. They are listed because they change how the
numbers should be read.

| # | Found by | Problem | Fix |
|---|---|---|---|
| 1 | Test: TIFF with two frames | `ImageSequence` reuses one image object, so every frame was OCR'd as the last frame | Each frame is converted and OCR'd as it is reached |
| 2 | Test: CSV line items | pandas inferred the column count from the first line and silently dropped wider item rows | CSV read with the `csv` module and padded to the widest row |
| 3 | Test: encrypted PDF | pdfminer wraps the password error; the outer message did not say "password" | Walk the exception cause chain |
| 4 | Eval: duplicate pair | Two duplicates on parallel workers: one worker's stale `completed` overwrote the other's `needs_review` | Cross-document step runs under one lock (OCR stays parallel); regression covered by `test_duplicate_invoice_flags_both_documents` |
| 5 | Eval: duplicate pair | Duplicate found only for the second upload; the first stayed `completed` | Siblings with the same number or file hash are re-validated after each document |
| 6 | Design review | A PDF and an Excel copy of the same invoice would have been reported as a duplicate, which defeats the brief's cross-verification goal | Decision table in `validation.py`: same file type → duplicate; different file type with agreeing values → verified |
| 7 | Eval: confidence | Every OCR-read supplier and customer name was flagged `LOW_FIELD_CONFIDENCE` (0.8 × 0.85 = 0.68 < 0.7). A label match is strong evidence | Label-matched names use 0.9 |
| 8 | Eval: harness | Final status was captured at the first terminal state, before a later duplicate changed it | Statuses re-read after the batch settles |
| 9 | Eval: dataset | An indentation error in the generator meant 17 of 20 digital PDFs were written but never labelled | Fixed; 72 labelled files now match the 72 files on disk |
| 10 | Eval: dataset | CSV writer did not quote values containing commas (`May 16, 2025`), producing a malformed row | Generator now uses a proper CSV writer. The parser is unchanged |

Items 9 and 10 are generator bugs, not system bugs. They are listed because they changed the dataset and
therefore the numbers.

## 5. Processing time

* Digital PDF: median 12 ms; XLSX ~10 ms; CSV ~4 ms.
* Clean scan (PNG): median 520 ms per page. Degraded scan: median 480 ms.
* Scanned PDF page (300 dpi render plus OCR): median 1,067 ms per page.

**1,000-page estimate (not benchmarked end to end).** At ~1.07 s per scanned page, a 1,000-page scanned
batch would take about 18 minutes on one worker and about 9 minutes on two. Two things would change that
in practice. The default `DI_MAX_OCR_PAGES=200` cap means a single PDF is OCR'd only up to 200 scanned
pages, and the rest are reported as a warning. Raise the cap for that workload. Also, digital PDF pages cost
roughly 1–2 ms each, so the estimate applies only to scanned pages. A real 1,000-page benchmark is part of
the remaining work.

## 6. Not measured, and why

| Item | Status |
|---|---|
| Accuracy on real customer invoices | Not measured. No real data was available for this build. The synthetic dataset is the only labelled set. |
| LLM extraction accuracy | Not measured. The code path is implemented and unit-tested with a mocked HTTP transport (grounding, fallback, malformed JSON, HTTP 429). It was not run against the live API. |
| Sustained load and concurrency | Not measured beyond the 72-file batch. |
| 1,000-page end-to-end run | Not measured. See the estimate in section 5. |
| Recorded walkthrough | Not produced by this build. It has to be recorded from the running UI. |

## 7. Reproduce

```bash
python eval/run_eval.py            # regenerates eval/dataset and eval/results
python -m pytest tests -q          # 88 tests
```
