"""OCR for scanned pages and photographed images (JPG, PNG, TIFF incl. multi-frame).

Pipeline: load -> grayscale -> upscale small scans -> denoise -> Otsu binarisation -> Tesseract.
Word-level confidences from Tesseract are aggregated into a document score so poor-quality
inputs can be flagged for manual review instead of silently producing wrong text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageSequence

from .errors import DocumentError

MAX_FRAMES = 20
MIN_WIDTH = 1600  # small scans are upscaled so Tesseract sees ~300 dpi glyphs


@dataclass
class OcrResult:
    text: str
    confidence: float  # 0-100, mean over recognised words
    frames: int = 1
    warnings: list[str] = field(default_factory=list)


def otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(float)
    total = hist.sum()
    sum_all = np.dot(np.arange(256), hist)
    sum_b, w_b, best, thresh = 0.0, 0.0, 0.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, thresh = between, t
    return thresh


def preprocess(img: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(img)
    if gray.width < MIN_WIDTH:
        scale = MIN_WIDTH / gray.width
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = gray.filter(ImageFilter.MedianFilter(3))
    arr = np.asarray(gray, dtype=np.uint8)
    binary = (arr > otsu_threshold(arr)).astype(np.uint8) * 255
    return Image.fromarray(binary, mode="L")


def ocr_image(img: Image.Image) -> tuple[str, float]:
    """Return (text, mean word confidence). Raises DocumentError if Tesseract is unavailable."""
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover - dependency check
        raise DocumentError("ocr_unavailable", "pytesseract is not installed", permanent=False) from exc
    try:
        data = pytesseract.image_to_data(preprocess(img), output_type=pytesseract.Output.DICT, config="--psm 3")
    except pytesseract.TesseractNotFoundError as exc:
        raise DocumentError("ocr_unavailable", "Tesseract binary not found on PATH", permanent=False) from exc

    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            continue
        confs.append(conf)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
    text = "\n".join(" ".join(words) for words in lines.values())
    confidence = float(np.mean(confs)) if confs else 0.0
    return text, confidence


def extract(path: Path) -> OcrResult:
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:
        raise DocumentError("corrupt_file", f"Image could not be decoded: {exc.__class__.__name__}") from exc

    texts, confs, warnings = [], [], []
    # ImageSequence reuses one image object for every frame, so each frame must be OCR'd as it is reached
    # (collecting frames into a list first would leave every entry pointing at the last frame).
    for index, frame in enumerate(ImageSequence.Iterator(img)):
        if index >= MAX_FRAMES:
            warnings.append(f"only first {MAX_FRAMES} TIFF frames processed")
            break
        text, conf = ocr_image(frame.convert("RGB"))
        texts.append(text)
        confs.append(conf)
    frames = texts

    confidence = float(np.mean(confs)) if confs else 0.0
    if not "".join(texts).strip():
        raise DocumentError("no_text_found", "OCR found no readable text in the image")
    if confidence < 30:
        warnings.append("very_low_ocr_confidence: image quality is likely too poor for automated extraction")
    elif confidence < 60:
        warnings.append("low_ocr_confidence: text may contain recognition errors")
    return OcrResult(text="\n\f".join(texts), confidence=confidence, frames=len(frames), warnings=warnings)
