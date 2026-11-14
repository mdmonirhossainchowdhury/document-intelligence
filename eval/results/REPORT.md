# Evaluation report (generated)

Samples: **72** (68 invoices, 4 corrupt). Dataset is synthetic (seed 7).

## Headline numbers

| Metric | Value |
|---|---|
| Document success rate (invoices processed to completed/needs_review) | 100.0% |
| Document failure rate (invoices ending `failed`) | 0.0% |
| Corrupt files correctly rejected | 4/4 |
| Overall field accuracy (8 fields, nulls count as correct) | 96.0% |
| Invented-value rate (populated where truth is absent) | 0.0% (0 values) |
| Line-item list exact match | 98.5% |
| Review flag vs design labels (genuine defects): precision / recall | 50.0% / 100.0% |
| Review flag vs measured extraction errors: precision / recall | 50.0% / 100.0% |
| Review flag vs combined rule (defect OR extraction error): precision / recall | 100.0% / 100.0% |
| Processing time per document (median / p95) | 11.0 ms / 998 ms |
| Batch wall-clock for all samples | 8.57 s (8.4 docs/s, 2 workers) |

## Field accuracy

| Field | Accuracy | Recall on populated | Invented |
|---|---|---|---|
| invoice_number | 92.7% | 94.1% | 0 |
| invoice_date | 97.1% | 97.1% | 0 |
| supplier_name | 97.1% | 98.5% | 0 |
| customer_name | 97.1% | 98.4% | 0 |
| subtotal | 91.2% | 91.2% | 0 |
| tax | 100.0% | 100.0% | 0 |
| total | 95.6% | 95.6% | 0 |
| currency | 97.1% | 97.1% | 0 |

## Per category

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

## Failure codes

```json
{
  "corrupt_file": 3,
  "type_mismatch": 1
}
```

## Validation rules triggered

```json
{
  "MISSING_FIELD": 23,
  "MISSING_REQUIRED_FIELD": 7,
  "CROSS_DOCUMENT_MATCH": 6,
  "LOW_FIELD_CONFIDENCE": 5,
  "TOTAL_MISMATCH": 3,
  "LINE_ITEM_ARITHMETIC": 2,
  "DUPLICATE_INVOICE": 2
}
```
