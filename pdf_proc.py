"""PDF processing: digital text layer first, OCR only for pages without one.

Handles multi-page documents (page-wise, with bounded OCR cost), encrypted files and
malformed streams. A hybrid PDF (some digital pages, some scanned pages) is handled per page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2 as pdfium

from . import image_proc
from .errors import DocumentError

OCR_DPI = 300


@dataclass
class PdfResult:
    text: str
    page_count: int
    text_pages: int
    ocr_pages: int
    ocr_confidence: float | None
    warnings: list[str] = field(default_factory=list)


def _page_texts(path: Path) -> tuple[list[str], int]:
    try:
        with pdfplumber.open(path) as pdf:
            if pdf.metadata.get("Encrypted") or getattr(pdf, "is_encrypted", False):
                raise DocumentError("encrypted_pdf", "Password-protected PDFs are not supported", permanent=True)
            return [(p.extract_text() or "") for p in pdf.pages], len(pdf.pages)
    except DocumentError:
        raise
    except Exception as exc:
        if _is_password_error(exc):
            raise DocumentError("encrypted_pdf", "Password-protected PDFs are not supported") from exc
        raise DocumentError("corrupt_file", f"PDF could not be parsed: {exc.__class__.__name__}") from exc


def _is_password_error(exc: BaseException) -> bool:
    """pdfminer wraps its password errors; walk the cause chain to find the underlying type."""
    seen: BaseException | None = exc
    while seen is not None:
        if "password" in type(seen).__name__.lower() or "password" in str(seen).lower():
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def extract(path: Path, max_ocr_pages: int, min_chars: int) -> PdfResult:
    texts, page_count = _page_texts(path)
    if page_count == 0:
        raise DocumentError("corrupt_file", "PDF contains no pages")

    warnings: list[str] = []
    ocr_pages, ocr_confs = 0, []
    need_ocr = [i for i, t in enumerate(texts) if len(t.strip()) < min_chars]

    if need_ocr:
        if len(need_ocr) > max_ocr_pages:
            warnings.append(f"OCR limited to first {max_ocr_pages} of {len(need_ocr)} scanned pages")
            need_ocr = need_ocr[:max_ocr_pages]
        doc = pdfium.PdfDocument(str(path))
        try:
            for i in need_ocr:
                bitmap = doc[i].render(scale=OCR_DPI / 72)
                page_text, conf = image_proc.ocr_image(bitmap.to_pil())
                if page_text.strip():
                    texts[i] = page_text
                    ocr_confs.append(conf)
                    ocr_pages += 1
                else:
                    warnings.append(f"page {i + 1}: no readable text after OCR")
        finally:
            doc.close()

    text_pages = page_count - len(need_ocr)
    confidence = float(np.mean(ocr_confs)) if ocr_confs else None
    if confidence is not None and confidence < 60:
        warnings.append("low_ocr_confidence: scanned pages may contain recognition errors")

    full_text = "\n\f".join(t for t in texts if t is not None)
    if not full_text.strip():
        raise DocumentError("no_text_found", "No text could be extracted from the PDF")
    return PdfResult(
        text=full_text,
        page_count=page_count,
        text_pages=text_pages,
        ocr_pages=ocr_pages,
        ocr_confidence=confidence,
        warnings=warnings,
    )
