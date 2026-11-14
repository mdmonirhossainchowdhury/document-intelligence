"""Evaluation harness: runs the real API over the labelled dataset and measures what the system does.

    python eval/run_eval.py            # builds the dataset (deterministic seed) and evaluates it
    python eval/run_eval.py --no-build # evaluates an existing eval/dataset

Outputs (written to eval/results/):
    metrics.json   every number reported in docs/EVALUATION.md
    per_document.csv  one row per sample: predicted vs expected
    REPORT.md      generated summary tables

Method
------
* Every file is uploaded through POST /api/documents in batches (as a client would), then polled
  until it reaches a terminal state. Wall-clock timings therefore include queueing and the worker.
* Field correctness: invoice_number (alphanumeric, case-insensitive), dates (exact), names (token-sort
  similarity >= 90), amounts (|diff| <= 0.01), currency (exact).
* A field the ground truth says is absent but the system populates is counted as an INVENTED value.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"

# Isolated data directory: must be set before the app is imported.
_DATA = tempfile.mkdtemp(prefix="docintel-eval-")
os.environ["DI_DATA_DIR"] = _DATA
os.environ["DI_MAX_FILES"] = "50"
os.environ.pop("ANTHROPIC_API_KEY", None)  # evaluate the deterministic rules path unless --llm is passed
os.environ.pop("DI_API_KEY", None)
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from rapidfuzz import fuzz  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from eval import dataset as dataset_gen  # noqa: E402
from app.main import app  # noqa: E402

FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
TERMINAL = {"completed", "needs_review", "failed"}
BATCH = 25


def _norm_id(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper()) or None


def field_match(name: str, expected, predicted) -> bool:
    if expected is None or predicted is None:
        return expected is None and predicted is None
    if name == "invoice_number":
        return _norm_id(expected) == _norm_id(predicted)
    if name in ("supplier_name", "customer_name"):
        return fuzz.token_sort_ratio(str(expected), str(predicted)) >= 90
    if name in ("subtotal", "tax", "total"):
        return abs(float(expected) - float(predicted)) <= 0.01
    return str(expected).strip().upper() == str(predicted).strip().upper()


def item_match(expected_items, predicted_items) -> bool:
    if len(expected_items) != len(predicted_items):
        return False
    return all(abs(e["amount"] - p["amount"]) <= 0.01 for e, p in zip(expected_items, predicted_items))


def run(build: bool, out_dir: Path, sample_limit: int | None) -> dict:
    if build or not (EVAL_DIR / "dataset" / "labels.json").exists():
        dataset_gen.build(EVAL_DIR / "dataset", seed=7)
    ds_dir = EVAL_DIR / "dataset"
    labels = json.loads((ds_dir / "labels.json").read_text(encoding="utf-8"))
    samples = labels["samples"][:sample_limit] if sample_limit else labels["samples"]

    results = []
    with TestClient(app) as client:
        wall_start = time.perf_counter()
        for start in range(0, len(samples), BATCH):
            chunk = samples[start:start + BATCH]
            files = [("files", (Path(s["path"]).name, (ds_dir / s["path"]).read_bytes(), "application/octet-stream"))
                     for s in chunk]
            resp = client.post("/api/documents", files=files)
            resp.raise_for_status()
            accepted = {a["filename"]: a["id"] for a in resp.json()["accepted"]}
            for s in chunk:
                name = Path(s["path"]).name
                results.append({"sample": s, "doc_id": accepted.get(name), "rejected": name not in accepted})

        pending = [r for r in results if r["doc_id"]]
        deadline = time.time() + 1800
        while pending and time.time() < deadline:
            still = []
            for r in pending:
                st = client.get(f"/api/documents/{r['doc_id']}/status").json()
                if st["status"] in TERMINAL:
                    r["final"] = st["status"]
                    r["error_code"] = st.get("error_code")
                else:
                    still.append(r)
            pending = still
            if pending:
                time.sleep(0.2)
        batch_wall_s = time.perf_counter() - wall_start

        # Cross-document rules can change a document AFTER it first reached a terminal state (e.g. a
        # duplicate that arrives later flags the earlier one). Re-read every final status once the whole
        # batch has settled, so the measurement reflects the system's state at quiescence.
        for r in results:
            if r.get("doc_id"):
                st = client.get(f"/api/documents/{r['doc_id']}/status").json()
                r["final"] = st["status"]
                r["error_code"] = st.get("error_code")

        for r in results:
            if not r.get("doc_id"):
                r["final"] = "rejected"
                continue
            doc = client.get(f"/api/documents/{r['doc_id']}").json()
            r["doc"] = doc
            r["extraction"] = doc["extraction"]
            r["issue_rules"] = sorted({i["rule"] for i in doc["issues"]})
            parse_ms = sum(e["duration_ms"] or 0 for e in doc["events"] if e["stage"] in ("parse", "extract"))
            r["processing_ms"] = parse_ms

    return score(results, batch_wall_s, labels, out_dir)


def score(results: list[dict], batch_wall_s: float, labels: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, per_cat = [], {}
    field_stats = {f: {"correct": 0, "total": 0, "invented": 0, "missed": 0, "populated_expected": 0} for f in FIELDS}
    line_items_exact, line_items_total = 0, 0
    review_tp = review_fp = review_fn = review_tn = 0
    err_tp = err_fp = err_fn = err_tn = 0
    cb_tp = cb_fp = cb_fn = cb_tn = 0
    corrupt_total = corrupt_caught = 0
    processed_ok = 0
    proc_ms = []
    invented_total, null_expected_total = 0, 0

    for r in results:
        s = r["sample"]
        cat = s["category"]
        bucket = per_cat.setdefault(cat, {"n": 0, "processed": 0, "failed": 0, "needs_review": 0, "field_correct": 0, "field_total": 0})
        bucket["n"] += 1
        final = r.get("final", "rejected")
        if final in ("completed", "needs_review"):
            bucket["processed"] += 1
        if final == "failed":
            bucket["failed"] += 1
        if final == "needs_review":
            bucket["needs_review"] += 1

        predicted_review = final == "needs_review" or final == "failed"
        if s["corrupt"]:
            corrupt_total += 1
            if final == "failed":
                corrupt_caught += 1
            rows.append(_row(s, r, final, None))
            continue

        if r.get("processing_ms"):
            proc_ms.append(r["processing_ms"])
        if final in ("completed", "needs_review"):
            processed_ok += 1

        # review-flag confusion matrix (invoice samples only), two references:
        #  (a) the design label in labels.json (what the fixture was built to exercise)
        #  (b) measured: the extracted record actually disagrees with the ground truth somewhere
        pred_for_err = r.get("extraction") or {}
        has_error = any(not field_match(f, s["expected"].get(f), pred_for_err.get(f) if pred_for_err.get("available") else None)
                        for f in FIELDS) or (
            s["expected"].get("line_items") is not None
            and not item_match(s["expected"]["line_items"], pred_for_err.get("line_items") or []))
        if s["expected_review"]:
            review_tp += predicted_review
            review_fn += not predicted_review
        else:
            review_fp += predicted_review
            review_tn += not predicted_review
        r["has_error"] = has_error
        should_review = bool(s["expected_review"]) or has_error  # genuine document defect OR extraction error
        if should_review:
            cb_tp += predicted_review
            cb_fn += not predicted_review
        else:
            cb_fp += predicted_review
            cb_tn += not predicted_review
        if has_error:
            err_tp += predicted_review
            err_fn += not predicted_review
        else:
            err_fp += predicted_review
            err_tn += not predicted_review

        pred = r.get("extraction") or {}
        for f in FIELDS:
            exp_v = s["expected"].get(f)
            pred_v = pred.get(f) if pred.get("available") else None
            ok = field_match(f, exp_v, pred_v)
            fs = field_stats[f]
            fs["total"] += 1
            fs["correct"] += ok
            bucket["field_total"] += 1
            bucket["field_correct"] += ok
            if exp_v is None:
                null_expected_total += 1
                if pred_v is not None:
                    fs["invented"] += 1
                    invented_total += 1
            else:
                fs["populated_expected"] += 1
                if pred_v is None:
                    fs["missed"] += 1

        if s["expected"].get("line_items") is not None:
            line_items_total += 1
            exp_items = s["expected"]["line_items"]
            pred_items = pred.get("line_items") or []
            line_items_exact += item_match(exp_items, pred_items)

        rows.append(_row(s, r, final, pred))

    total_docs = len(results)
    invoice_docs = sum(1 for r in results if not r["sample"]["corrupt"])
    metrics = {
        "dataset": {"samples": total_docs, "invoice_samples": invoice_docs, "corrupt_samples": corrupt_total,
                    "seed": labels.get("seed")},
        "document_success_rate": round(processed_ok / invoice_docs, 4) if invoice_docs else None,
        "document_failure_rate": round(sum(1 for r in results if r.get("final") == "failed" and not r["sample"]["corrupt"]) / invoice_docs, 4) if invoice_docs else None,
        "corrupt_files_correctly_rejected": f"{corrupt_caught}/{corrupt_total}",
        "overall_field_accuracy": round(sum(fs["correct"] for fs in field_stats.values()) / sum(fs["total"] for fs in field_stats.values()), 4),
        "field_accuracy": {f: round(fs["correct"] / fs["total"], 4) for f, fs in field_stats.items()},
        "field_recall_on_populated": {f: round((fs["populated_expected"] - fs["missed"]) / fs["populated_expected"], 4)
                                      for f, fs in field_stats.items() if fs["populated_expected"]},
        "invented_value_rate": round(invented_total / null_expected_total, 4) if null_expected_total else 0.0,
        "invented_values": invented_total,
        "field_invented": {f: fs["invented"] for f, fs in field_stats.items()},
        "line_item_list_exact_match": round(line_items_exact / line_items_total, 4) if line_items_total else None,
        "review_flagging": {
            "precision": round(review_tp / (review_tp + review_fp), 4) if (review_tp + review_fp) else None,
            "recall": round(review_tp / (review_tp + review_fn), 4) if (review_tp + review_fn) else None,
            "false_flag_rate_on_clean": round(review_fp / (review_fp + review_tn), 4) if (review_fp + review_tn) else None,
            "tp": review_tp, "fp": review_fp, "fn": review_fn, "tn": review_tn,
        },
        "review_flagging_combined": {
            "description": "should_review = design defect OR measured extraction error",
            "precision": round(cb_tp / (cb_tp + cb_fp), 4) if (cb_tp + cb_fp) else None,
            "recall": round(cb_tp / (cb_tp + cb_fn), 4) if (cb_tp + cb_fn) else None,
            "false_flag_rate_on_correct_records": round(cb_fp / (cb_fp + cb_tn), 4) if (cb_fp + cb_tn) else None,
            "tp": cb_tp, "fp": cb_fp, "fn": cb_fn, "tn": cb_tn,
        },
        "review_flagging_vs_measured_errors": {
            "precision": round(err_tp / (err_tp + err_fp), 4) if (err_tp + err_fp) else None,
            "recall": round(err_tp / (err_tp + err_fn), 4) if (err_tp + err_fn) else None,
            "false_flag_rate_on_correct_records": round(err_fp / (err_fp + err_tn), 4) if (err_fp + err_tn) else None,
            "tp": err_tp, "fp": err_fp, "fn": err_fn, "tn": err_tn,
        },
        "processing_time_ms": {
            "per_document_mean": round(statistics.mean(proc_ms), 1) if proc_ms else None,
            "per_document_median": round(statistics.median(proc_ms), 1) if proc_ms else None,
            "per_document_p95": round(sorted(proc_ms)[int(0.95 * (len(proc_ms) - 1))], 1) if proc_ms else None,
        },
        "batch_wall_clock_s": round(batch_wall_s, 2),
        "throughput_docs_per_s": round(total_docs / batch_wall_s, 2) if batch_wall_s else None,
        "per_category": per_cat,
        "failure_codes": _count([r.get("error_code") for r in results if r.get("final") == "failed"]),
        "rules_triggered": _count([rule for r in results for rule in r.get("issue_rules", [])]),
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    with (out_dir / "per_document.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "REPORT.md").write_text(_report(metrics), encoding="utf-8")
    return metrics


def _row(s, r, final, pred) -> dict:
    pred = pred or {}
    return {
        "sample": s["id"], "category": s["category"], "final_status": final,
        "error_code": r.get("error_code") or "", "expected_review": s["expected_review"],
        "rules_triggered": ";".join(r.get("issue_rules", [])),
        "pred_invoice_number": pred.get("invoice_number"), "exp_invoice_number": s["expected"].get("invoice_number"),
        "pred_total": pred.get("total"), "exp_total": s["expected"].get("total"),
        "pred_supplier": pred.get("supplier_name"), "exp_supplier": s["expected"].get("supplier_name"),
        "processing_ms": r.get("processing_ms", ""),
        "measured_field_error": r.get("has_error", ""),
    }


def _count(items) -> dict:
    out: dict[str, int] = {}
    for i in items:
        if i:
            out[i] = out.get(i, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _report(m: dict) -> str:
    lines = [
        "# Evaluation report (generated)",
        "",
        f"Samples: **{m['dataset']['samples']}** ({m['dataset']['invoice_samples']} invoices, {m['dataset']['corrupt_samples']} corrupt). Dataset is synthetic (seed {m['dataset']['seed']}).",
        "",
        "## Headline numbers",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Document success rate (invoices processed to completed/needs_review) | {_pct(m['document_success_rate'])} |",
        f"| Document failure rate (invoices ending `failed`) | {_pct(m['document_failure_rate'])} |",
        f"| Corrupt files correctly rejected | {m['corrupt_files_correctly_rejected']} |",
        f"| Overall field accuracy (8 fields, nulls count as correct) | {_pct(m['overall_field_accuracy'])} |",
        f"| Invented-value rate (populated where truth is absent) | {_pct(m['invented_value_rate'])} ({m['invented_values']} values) |",
        f"| Line-item list exact match | {_pct(m['line_item_list_exact_match'])} |",
        f"| Review flag vs design labels (genuine defects): precision / recall | {_pct(m['review_flagging']['precision'])} / {_pct(m['review_flagging']['recall'])} |",
        f"| Review flag vs measured extraction errors: precision / recall | {_pct(m['review_flagging_vs_measured_errors']['precision'])} / {_pct(m['review_flagging_vs_measured_errors']['recall'])} |",
        f"| Review flag vs combined rule (defect OR extraction error): precision / recall | {_pct(m['review_flagging_combined']['precision'])} / {_pct(m['review_flagging_combined']['recall'])} |",
        f"| Processing time per document (median / p95) | {m['processing_time_ms']['per_document_median']} ms / {m['processing_time_ms']['per_document_p95']} ms |",
        f"| Batch wall-clock for all samples | {m['batch_wall_clock_s']} s ({m['throughput_docs_per_s']} docs/s, 2 workers) |",
        "",
        "## Field accuracy",
        "",
        "| Field | Accuracy | Recall on populated | Invented |",
        "|---|---|---|---|",
    ]
    for f, acc in m["field_accuracy"].items():
        rec = m["field_recall_on_populated"].get(f)
        lines.append(f"| {f} | {_pct(acc)} | {_pct(rec)} | {m['field_invented'][f]} |")
    lines += ["", "## Per category", "", "| Category | Samples | Processed | Failed | Flagged for review | Field accuracy |", "|---|---|---|---|---|---|"]
    for cat, b in m["per_category"].items():
        acc = b["field_correct"] / b["field_total"] if b["field_total"] else None
        lines.append(f"| {cat} | {b['n']} | {b['processed']} | {b['failed']} | {b['needs_review']} | {_pct(acc)} |")
    lines += ["", "## Failure codes", "", "```json", json.dumps(m["failure_codes"], indent=2), "```",
              "", "## Validation rules triggered", "", "```json", json.dumps(m["rules_triggered"], indent=2), "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true", help="reuse eval/dataset instead of regenerating it")
    parser.add_argument("--limit", type=int, default=None, help="evaluate only the first N samples")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()
    try:
        metrics = run(build=not args.no_build, out_dir=args.out, sample_limit=args.limit)
    finally:
        shutil.rmtree(_DATA, ignore_errors=True)
    print(json.dumps({k: metrics[k] for k in ("document_success_rate", "overall_field_accuracy",
                                              "invented_value_rate", "review_flagging", "processing_time_ms",
                                              "throughput_docs_per_s")}, indent=2))
    print(f"\nwrote {args.out / 'metrics.json'} and {args.out / 'REPORT.md'}")


if __name__ == "__main__":
    main()
