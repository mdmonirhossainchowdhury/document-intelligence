"""Runtime configuration, read from environment variables with safe defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DI_DATA_DIR", BASE_DIR / "data")))
    db_path: Path | None = None  # derived from data_dir when None
    max_upload_bytes: int = _int("DI_MAX_UPLOAD_MB", 25) * 1024 * 1024
    max_files_per_request: int = _int("DI_MAX_FILES", 50)
    max_ocr_pages: int = _int("DI_MAX_OCR_PAGES", 200)
    ocr_min_text_chars: int = _int("DI_OCR_MIN_CHARS", 20)  # below this a PDF page is treated as scanned
    worker_threads: int = _int("DI_WORKERS", 2)
    max_attempts: int = _int("DI_MAX_ATTEMPTS", 2)
    cors_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.getenv("DI_CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    )
    # Optional LLM extraction. Rule-based extraction is used when no key is set or the call fails.
    # When set, every /api request must send the header `X-API-Key: <value>`.
    api_key: str | None = os.getenv("DI_API_KEY") or None
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
    llm_model: str = os.getenv("DI_LLM_MODEL", "claude-haiku-5-5")
    llm_timeout_s: float = float(os.getenv("DI_LLM_TIMEOUT", "30"))

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def database_path(self) -> Path:
        return self.db_path or (self.data_dir / "app.db")


SUPPORTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".tif": "image",
    ".tiff": "image",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".csv": "spreadsheet",
}

settings = Settings()
