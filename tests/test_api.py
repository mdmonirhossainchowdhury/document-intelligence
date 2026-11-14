"""REST API tests: upload, processing status, results, corrections, search, reprocessing, security."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from app import main as main_module
from app.config import settings
from eval import dataset as ds

from conftest import requires_tesseract

TERMINAL = {"completed", "needs_review", "failed"}


def _upload(client, paths, headers=None):
    files = [("files", (p.name, p.read_bytes(), "application/octet-stream")) for p in paths]
    return client.post("/api/documents", files=files, headers=headers or {})


def _wait(client, doc_id: str, timeout: float = 120.0, headers=None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/documents/{doc_id}/status", headers=headers or {}).json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.1)
    raise AssertionError(f"document {doc_id} did not finish in {timeout}s")


def _invoice_text(number: str, supplier: str = "Test Supplier Co", total_override: float | None = None) -> bytes:
    """A tiny, fully-specified invoice rendered as a digital PDF via the dataset renderer."""
    import tempfile

    inv = ds.Invoice(number=number, issued=ds.date(2025, 5, 20), supplier=supplier, customer="Client Ltd",
                     currency="USD", currency_token="USD", items=[("Consulting services", 2, 150.0)],
                     tax_rate=10.0, number_label="Invoice No:", date_label="Invoice Date:", date_style=0,
                     total_label="Total:")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "inv.pdf"
        ds.write_digital_pdf(target, inv.lines(total_override=total_override))
        return target.read_bytes()


def test_health_and_docs_available(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/openapi.json").status_code == 200


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_unsupported_upload_is_rejected_without_queueing(client, tmp_path):
    f = tmp_path / "contract.docx"
    f.write_bytes(b"PK\x03\x04...")
    r = _upload(client, [f])
    body = r.json()
    assert r.status_code == 202
    assert body["accepted"] == [] and body["rejected"][0]["filename"] == "contract.docx"


def test_empty_file_is_rejected(client, tmp_path):
    f = tmp_path / "empty.pdf"
    f.write_bytes(b"")
    body = _upload(client, [f]).json()
    assert body["rejected"][0]["reason"] == "file is empty"


def test_path_traversal_in_filename_is_neutralised(client):
    r = client.post("/api/documents", files=[("files", ("../../etc/evil.csv", b"a,b\n1,2\n", "text/csv"))])
    accepted = r.json()["accepted"]
    assert accepted and "/" not in accepted[0]["filename"] and ".." not in accepted[0]["filename"]


def test_oversize_upload_is_rejected(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "settings", replace(settings, max_upload_bytes=1024))
    f = tmp_path / "big.csv"
    f.write_bytes(b"x,y\n" + b"1,2\n" * 1000)
    body = _upload(client, [f]).json()
    assert body["rejected"] and "limit" in body["rejected"][0]["reason"]


@requires_tesseract
def test_end_to_end_digital_invoice_flow(client, tmp_path):
    pdf = tmp_path / "e2e_invoice.pdf"
    pdf.write_bytes(_invoice_text("E2E-7001", supplier="Zenith Parts Ltd"))
    accepted = _upload(client, [pdf]).json()["accepted"]
    assert accepted[0]["status"] == "queued"
    doc_id = accepted[0]["id"]

    status = _wait(client, doc_id)
    assert status["status"] == "completed", status

    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["extraction"]["invoice_number"] == "E2E-7001"
    assert doc["extraction"]["supplier_name"] == "Zenith Parts Ltd"
    assert doc["extraction"]["total"] == 330.0
    assert doc["extraction"]["tax"] == 30.0
    assert doc["extraction"]["currency"] == "USD"
    assert doc["extraction"]["line_items"][0]["amount"] == 300.0
    assert any(e["stage"] == "parse" for e in doc["events"])
    assert doc["text_preview"] and "E2E-7001" in doc["text_preview"]


def test_corrupt_upload_fails_permanently_without_retry(client, tmp_path):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.7\n" + b"\x00\xff" * 500)
    doc_id = _upload(client, [f]).json()["accepted"][0]["id"]
    status = _wait(client, doc_id)
    assert status["status"] == "failed"
    assert status["error_code"] == "corrupt_file"
    assert status["attempts"] == 1  # permanent errors are not retried


def test_mismatched_extension_fails_with_clear_code(client, tmp_path):
    f = tmp_path / "fake.png"
    f.write_text("just some plain text, with commas, not an image\n")
    doc_id = _upload(client, [f]).json()["accepted"][0]["id"]
    status = _wait(client, doc_id)
    assert status["status"] == "failed" and status["error_code"] == "type_mismatch"


def test_manual_correction_revalidates_and_clears_review(client, tmp_path):
    pdf = tmp_path / "bad_arith.pdf"
    pdf.write_bytes(_invoice_text("FIX-9001", supplier="Fixable Supplier", total_override=999.0))
    doc_id = _upload(client, [pdf]).json()["accepted"][0]["id"]
    status = _wait(client, doc_id)
    assert status["status"] == "needs_review"
    issues = client.get(f"/api/documents/{doc_id}/issues").json()["items"]
    assert any(i["rule"] == "TOTAL_MISMATCH" for i in issues)

    r = client.patch(f"/api/documents/{doc_id}/extraction", json={"total": 330.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["extraction"]["total"] == 330.0
    assert "total" in body["extraction"]["corrected_fields"]
    assert not any(i["rule"] == "TOTAL_MISMATCH" for i in body["issues"])


def test_correction_rejects_unknown_fields_and_bad_currency(client, tmp_path):
    pdf = tmp_path / "strict.pdf"
    pdf.write_bytes(_invoice_text("STRICT-1"))
    doc_id = _upload(client, [pdf]).json()["accepted"][0]["id"]
    _wait(client, doc_id)
    assert client.patch(f"/api/documents/{doc_id}/extraction", json={"status": "completed"}).status_code == 422
    assert client.patch(f"/api/documents/{doc_id}/extraction", json={"currency": "dollars"}).status_code == 422
    assert client.patch(f"/api/documents/{doc_id}/extraction", json={}).status_code == 422


def test_duplicate_invoice_flags_both_documents(client, tmp_path):
    a, b = tmp_path / "dup_a.pdf", tmp_path / "dup_b.pdf"
    a.write_bytes(_invoice_text("DUPAPI-55", supplier="Dup Supplier"))
    b.write_bytes(_invoice_text("DUPAPI-55", supplier="Dup Supplier", total_override=None))
    ids = [x["id"] for x in _upload(client, [a, b]).json()["accepted"]]
    statuses = [_wait(client, i)["status"] for i in ids]
    assert statuses == ["needs_review", "needs_review"]
    for doc_id in ids:
        rules = {i["rule"] for i in client.get(f"/api/documents/{doc_id}/issues").json()["items"]}
        assert "DUPLICATE_INVOICE" in rules


def test_identical_file_is_reported_as_duplicate_of(client, tmp_path):
    f = tmp_path / "same_bytes.pdf"
    f.write_bytes(_invoice_text("SAME-BYTES-1"))
    first = _upload(client, [f]).json()["accepted"][0]["id"]
    _wait(client, first)
    second = _upload(client, [f]).json()["accepted"][0]
    assert second["duplicate_of"] == first
    _wait(client, second["id"])


def test_keyword_and_semantic_search_find_processed_document(client, tmp_path):
    f = tmp_path / "searchable.csv"
    f.write_text("Invoice Number,SRCH-42\nSupplier,Quantum Lighting Works\nTotal,500.00\nCurrency,USD\n"
                 "\nDescription,Qty,Unit Price,Amount\nLED panel,5,100.00,500.00\n")
    doc_id = _upload(client, [f]).json()["accepted"][0]["id"]
    assert _wait(client, doc_id)["status"] in ("completed", "needs_review")

    kw = client.get("/api/search", params={"q": "Quantum Lighting", "mode": "keyword"}).json()["items"]
    assert kw and kw[0]["id"] == doc_id and kw[0]["snippet"]
    sem = client.get("/api/search", params={"q": "lighting supplier invoice", "mode": "semantic"}).json()["items"]
    assert any(item["id"] == doc_id for item in sem)
    hyb = client.get("/api/search", params={"q": "SRCH-42", "mode": "hybrid"}).json()["items"]
    assert hyb and hyb[0]["id"] == doc_id


def test_search_rejects_empty_query(client):
    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_list_filters_by_supplier_and_status(client):
    rows = client.get("/api/documents", params={"supplier": "Quantum Lighting"}).json()
    assert rows["total"] >= 1 and all("Quantum" in (r["supplier_name"] or "") for r in rows["items"])
    failed = client.get("/api/documents", params={"status": "failed"}).json()
    assert all(r["status"] == "failed" for r in failed["items"])


def test_list_rejects_invalid_status_filter(client):
    assert client.get("/api/documents", params={"status": "bogus"}).status_code == 422


def test_download_original_file(client, tmp_path):
    f = tmp_path / "download_me.csv"
    f.write_text("Invoice Number,DL-1\nTotal,1.00\n")
    doc_id = _upload(client, [f]).json()["accepted"][0]["id"]
    _wait(client, doc_id)
    r = client.get(f"/api/documents/{doc_id}/file")
    assert r.status_code == 200 and r.content == f.read_bytes()


def test_reprocess_failed_document_after_transient_outage(client, tmp_path, monkeypatch):
    """Transient OCR outage: the job retries, fails after max attempts, and can be reprocessed later."""
    import pytesseract

    img = tmp_path / "outage.png"
    ds.render_scan(["INVOICE", "Invoice No: OUT-1", "Total: 5.00"], degrade=False, seed=3).save(img)

    real = pytesseract.image_to_data

    def down(*_a, **_k):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", down)
    doc_id = _upload(client, [img]).json()["accepted"][0]["id"]
    status = _wait(client, doc_id, timeout=60)
    assert status["status"] == "failed" and status["error_code"] == "processing_failed"
    assert status["attempts"] == settings.max_attempts

    monkeypatch.setattr(pytesseract, "image_to_data", real)
    r = client.post(f"/api/documents/{doc_id}/reprocess")
    assert r.status_code == 202
    assert _wait(client, doc_id, timeout=120)["status"] in ("completed", "needs_review")


def test_reprocess_rejects_document_already_in_queue(client, tmp_path):
    f = tmp_path / "busy.csv"
    f.write_text("Invoice Number,BUSY-1\nTotal,2.00\n")
    doc_id = _upload(client, [f]).json()["accepted"][0]["id"]
    _wait(client, doc_id)
    from app.processing import set_status
    set_status(doc_id, "processing")
    assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 409
    set_status(doc_id, "completed")


def test_delete_removes_document_and_index(client, tmp_path):
    f = tmp_path / "to_delete.csv"
    f.write_text("Invoice Number,DEL-1\nSupplier,Doomed Corp\nTotal,3.00\n")
    doc_id = _upload(client, [f]).json()["accepted"][0]["id"]
    _wait(client, doc_id)
    assert client.delete(f"/api/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    hits = client.get("/api/search", params={"q": "Doomed Corp", "mode": "keyword"}).json()["items"]
    assert not any(h["id"] == doc_id for h in hits)


def test_unknown_document_returns_404(client):
    assert client.get("/api/documents/does-not-exist").status_code == 404


def test_api_key_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(main_module, "settings", replace(settings, api_key="s3cret"))
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/stats", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/health").status_code == 200  # liveness probe stays open


def test_stats_reports_failure_rate(client):
    stats = client.get("/api/stats").json()
    assert 0.0 <= stats["failure_rate"] <= 1.0
    assert stats["total_documents"] >= 1
