"""Document processing pipeline: format coverage, OCR, spreadsheets, and failure scenarios."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.pipeline import image_proc
from app.pipeline.errors import DocumentError
from app.pipeline.ingest import detect, process_file
from app.pipeline import sheet_proc
from eval import dataset as ds

from conftest import requires_tesseract


def _noop(*_args):
    return None


# --------------------------------------------------------------------------- type detection

def test_detects_each_supported_type(files_dir: Path):
    assert detect(files_dir / "digital_00.pdf", "digital_00.pdf") == ("pdf", "pdf")
    assert detect(files_dir / "image_00.png", "image_00.png")[0] == "image"
    assert detect(files_dir / "sheet_00.xlsx", "sheet_00.xlsx") == ("spreadsheet", "xlsx")
    assert detect(files_dir / "sheet_00.csv", "sheet_00.csv") == ("spreadsheet", "csv")


def test_extension_content_mismatch_is_rejected(files_dir: Path):
    with pytest.raises(DocumentError) as exc:
        detect(files_dir / "wrong_extension.png", "wrong_extension.png")
    assert exc.value.code == "type_mismatch" and exc.value.permanent


def test_unsupported_extension_rejected(tmp_path: Path):
    f = tmp_path / "notes.docx"
    f.write_bytes(b"PK\x03\x04whatever")
    with pytest.raises(DocumentError) as exc:
        detect(f, "notes.docx")
    assert exc.value.code == "unsupported_type"


def test_binary_garbage_is_corrupt(files_dir: Path):
    with pytest.raises(DocumentError) as exc:
        detect(files_dir / "garbage.xlsx", "garbage.xlsx")
    assert exc.value.code == "corrupt_file"


# --------------------------------------------------------------------------- PDFs

def test_multipage_digital_pdf_text_extraction(files_dir: Path):
    content = process_file(files_dir / "digital_00.pdf", "digital_00.pdf", _noop)
    assert content.page_count == 2  # digital_00 has an extra terms page
    assert "Terms and conditions (page 2)" in content.text
    assert content.source_method == "pdf_text"
    assert content.ocr_confidence is None


@requires_tesseract
def test_scanned_pdf_uses_ocr_path(files_dir: Path):
    content = process_file(files_dir / "scanned_pdf_00.pdf", "scanned_pdf_00.pdf", _noop)
    assert content.source_method == "pdf_ocr"
    assert content.ocr_confidence and content.ocr_confidence > 50
    assert "INVOICE" in content.text.upper()


def test_encrypted_pdf_is_permanent_failure(tmp_path: Path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from reportlab.lib.pdfencrypt import StandardEncryption

    target = tmp_path / "locked.pdf"
    enc = StandardEncryption(userPassword="user-pw", ownerPassword="owner-pw", canPrint=1)
    c = canvas.Canvas(str(target), pagesize=A4, encrypt=enc)
    c.drawString(72, 720, "Invoice No: LOCK-1")
    c.save()
    with pytest.raises(DocumentError) as exc:
        process_file(target, "locked.pdf", _noop)
    assert exc.value.code == "encrypted_pdf" and exc.value.permanent


def test_truncated_pdf_fails_cleanly(files_dir: Path):
    with pytest.raises(DocumentError) as exc:
        process_file(files_dir / "truncated.pdf", "truncated.pdf", _noop)
    assert exc.value.code in ("corrupt_file", "no_text_found")


@requires_tesseract
def test_blank_pdf_yields_no_text(tmp_path: Path):
    from reportlab.pdfgen import canvas

    target = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(target))
    c.showPage()  # one empty page: no text layer, OCR finds nothing
    c.save()
    with pytest.raises(DocumentError) as exc:
        process_file(target, "empty.pdf", _noop)
    assert exc.value.code == "no_text_found"


# --------------------------------------------------------------------------- images / OCR

@requires_tesseract
def test_clean_png_ocr_recovers_invoice_number(labels, files_dir: Path):
    sample = next(s for s in labels["samples"] if s["id"] == "image_00.png")
    content = process_file(files_dir / "image_00.png", "image_00.png", _noop)
    assert content.source_method == "image_ocr"
    assert sample["expected"]["invoice_number"].split("-")[-1] in content.text
    assert content.ocr_confidence > 60


@requires_tesseract
def test_multiframe_tiff_is_processed_per_frame(tmp_path: Path):
    frames = [ds.render_scan(["INVOICE", f"Invoice No: TIFF-{n}", "Total: 100.00"], degrade=False, seed=n)
              for n in (1, 2)]
    target = tmp_path / "multi.tiff"
    frames[0].save(target, save_all=True, append_images=frames[1:])
    content = process_file(target, "multi.tiff", _noop)
    assert content.page_count == 2
    assert "TIFF-1" in content.text and "TIFF-2" in content.text


def test_blank_image_is_rejected(tmp_path: Path):
    target = tmp_path / "blank.png"
    Image.new("RGB", (800, 600), "white").save(target)
    with pytest.raises(DocumentError) as exc:
        image_proc.extract(target)
    assert exc.value.code == "no_text_found"


def test_otsu_threshold_separates_bimodal_histogram():
    import numpy as np

    arr = np.array([10] * 500 + [240] * 500, dtype=np.uint8).reshape(-1, 10)
    t = image_proc.otsu_threshold(arr)
    assert 10 <= t < 240


def test_ocr_unavailable_is_transient(monkeypatch, files_dir: Path):
    import pytesseract

    def boom(*_a, **_k):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    with pytest.raises(DocumentError) as exc:
        image_proc.ocr_image(Image.new("RGB", (100, 100), "white"))
    assert exc.value.code == "ocr_unavailable" and not exc.value.permanent


# --------------------------------------------------------------------------- spreadsheets

def test_multisheet_xlsx_parses_every_sheet(files_dir: Path):
    content = process_file(files_dir / "sheet_00.xlsx", "sheet_00.xlsx", _noop)
    assert content.page_count == 2
    assert "[Sheet: Invoice]" in content.text and "[Sheet: Line Items]" in content.text
    assert "Invoice Number: INV-" in content.text
    assert content.tables[1][0] == ["Description", "Qty", "Unit Price", "Amount"]


def test_csv_with_variable_width_rows_keeps_line_items(files_dir: Path, labels):
    content = process_file(files_dir / "sheet_00.csv", "sheet_00.csv", _noop)
    expected_items = next(s for s in labels["samples"] if s["id"] == "sheet_00.csv")["expected"]["line_items"]
    rows = content.tables[0]
    assert len(rows) >= len(expected_items) + 2  # key/value header rows + items
    assert any(row and row[0] == expected_items[0]["description"] for row in rows)


def test_csv_encoding_fallback(tmp_path: Path):
    target = tmp_path / "latin.csv"
    target.write_bytes("Invoice Number,INV-ü-1\nSupplier,Café Ltd\nTotal,10.00\n".encode("cp1252"))
    content = process_file(target, "latin.csv", _noop)
    assert "Café Ltd" in content.text


def test_empty_csv_has_no_data(tmp_path: Path):
    target = tmp_path / "empty.csv"
    target.write_text(",,\n,,\n")
    with pytest.raises(DocumentError) as exc:
        process_file(target, "empty.csv", _noop)
    assert exc.value.code == "no_text_found"


def test_sheet_row_limit_truncates_with_warning(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sheet_proc, "MAX_ROWS_PER_SHEET", 3)
    target = tmp_path / "long.csv"
    target.write_text("\n".join(f"row{i},{i}" for i in range(10)))
    result = sheet_proc.extract(target, "csv")
    assert any("truncated" in w for w in result.warnings)


# --------------------------------------------------------------------------- failure scenarios

def test_pipeline_never_leaks_unexpected_exceptions_as_success(tmp_path: Path, monkeypatch):
    target = tmp_path / "x.csv"
    target.write_text("a,b\n1,2\n")

    def explode(*_a, **_k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(sheet_proc, "extract", explode)
    with pytest.raises(DocumentError) as exc:
        process_file(target, "x.csv", _noop)
    assert exc.value.code == "corrupt_file"
