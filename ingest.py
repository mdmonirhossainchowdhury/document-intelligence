"""Type detection and dispatch to the format-specific processors.

Detection never trusts the file extension alone: the leading bytes ("magic numbers") must match
the claimed type, otherwise the file is treated as corrupt or mismatched.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import SUPPORTED_EXTENSIONS, settings
from . import image_proc, pdf_proc, sheet_proc
from .errors import DocumentError

log = logging.getLogger(__name__)


@dataclass
class ExtractedContent:
    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)  # [sheet/table][row][cell]
    source_method: str = ""
    page_count: int | None = None
    ocr_confidence: float | None = None  # 0-100, None when no OCR was used
    warnings: list[str] = field(default_factory=list)


_SIGNATURES = {
    "pdf": [b"%PDF-"],
    "image_png": [b"\x89PNG\r\n\x1a\n"],
    "image_jpeg": [b"\xff\xd8\xff"],
    "image_tiff": [b"II*\x00", b"MM\x00*"],
    "xlsx": [b"PK\x03\x04"],
    "xls": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
}


def _read_head(path: Path, n: int) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def sniff(path: Path) -> str | None:
    head = _read_head(path, 8)
    for kind, sigs in _SIGNATURES.items():
        if any(head.startswith(s) for s in sigs):
            return kind
    if _looks_like_text(path):
        return "csv"
    return None


def _looks_like_text(path: Path) -> bool:
    raw = _read_head(path, 4096)
    if not raw or b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            raw.decode("latin-1")
            return raw.count(b",") > 0 or raw.count(b"\t") > 0
        except UnicodeDecodeError:
            return False


def detect(path: Path, filename: str) -> tuple[str, str]:
    """Return (file_type, sniffed_kind). Raises DocumentError for unsupported or mismatched files."""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise DocumentError("unsupported_type", f"Unsupported file extension '{ext or 'none'}'")
    file_type = SUPPORTED_EXTENSIONS[ext]
    kind = sniff(path)
    if kind is None:
        raise DocumentError("corrupt_file", "File content is not a recognised document (corrupt or empty)")

    expected_ok = {
        "pdf": kind == "pdf",
        "image": kind.startswith("image_"),
        "spreadsheet": (ext == ".xlsx" and kind == "xlsx")
        or (ext == ".xls" and kind == "xls")
        or (ext == ".csv" and kind == "csv"),
    }[file_type]
    if not expected_ok:
        raise DocumentError(
            "type_mismatch",
            f"File extension '{ext}' does not match detected content '{kind}'",
        )
    if file_type == "image" and kind == "image_tiff" and ext not in (".tif", ".tiff"):
        raise DocumentError("type_mismatch", "TIFF content under a non-TIFF extension")
    return file_type, kind


def process_file(path: Path, filename: str, emit: Callable[[str, str, str, int], None]) -> ExtractedContent:
    """Run the appropriate pipeline. `emit(stage, status, message, duration_ms)` records progress."""
    file_type, kind = detect(path, filename)
    handlers = {"pdf": _run_pdf, "image": _run_image, "spreadsheet": _run_sheet}
    started = time.perf_counter()
    try:
        content = handlers[file_type](path, kind, emit)
    except DocumentError:
        raise
    except Exception as exc:  # parser crashes on malformed input are corrupt-file failures
        log.warning("parser failure for %s: %s", filename, exc)
        raise DocumentError("corrupt_file", f"Could not parse file: {exc.__class__.__name__}") from exc
    emit("parse", "ok", f"{file_type} parsed in {int((time.perf_counter() - started) * 1000)} ms", int((time.perf_counter() - started) * 1000))
    return content


def _run_pdf(path: Path, kind: str, emit) -> ExtractedContent:
    started = time.perf_counter()
    result = pdf_proc.extract(path, max_ocr_pages=settings.max_ocr_pages, min_chars=settings.ocr_min_text_chars)
    emit("pdf_text", "ok", f"{result.page_count} pages, {result.ocr_pages} OCR'd", int((time.perf_counter() - started) * 1000))
    return ExtractedContent(
        text=result.text,
        source_method="pdf_ocr" if result.ocr_pages else "pdf_text",
        page_count=result.page_count,
        ocr_confidence=result.ocr_confidence,
        warnings=result.warnings,
    )


def _run_image(path: Path, kind: str, emit) -> ExtractedContent:
    started = time.perf_counter()
    result = image_proc.extract(path)
    emit("ocr", "ok", f"OCR confidence {result.confidence:.0f}/100", int((time.perf_counter() - started) * 1000))
    return ExtractedContent(
        text=result.text,
        source_method="image_ocr",
        page_count=result.frames,
        ocr_confidence=result.confidence,
        warnings=result.warnings,
    )


def _run_sheet(path: Path, kind: str, emit) -> ExtractedContent:
    started = time.perf_counter()
    result = sheet_proc.extract(path, kind)
    emit("spreadsheet", "ok", f"{len(result.tables)} table(s) parsed", int((time.perf_counter() - started) * 1000))
    return ExtractedContent(
        text=result.text,
        tables=result.tables,
        source_method="csv" if kind == "csv" else "spreadsheet",
        page_count=len(result.tables),
        warnings=result.warnings,
    )
