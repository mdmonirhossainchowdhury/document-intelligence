"""Shared fixtures. The data directory is a fresh temp dir per test session, set BEFORE the app is imported
because `app.config.settings` is read at import time."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

_DATA_DIR = tempfile.mkdtemp(prefix="docintel-test-")
os.environ["DI_DATA_DIR"] = _DATA_DIR
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests never call a real LLM
os.environ.pop("DI_API_KEY", None)

import pytest  # noqa: E402

from eval import dataset as dataset_gen  # noqa: E402


def pytest_sessionfinish(session, exitstatus):  # cleanup temp data
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


requires_tesseract = pytest.mark.skipif(not tesseract_available(), reason="tesseract binary not installed")


@pytest.fixture(scope="session")
def labelled_dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dataset")
    dataset_gen.build(out, seed=7)
    return out


@pytest.fixture(scope="session")
def files_dir(labelled_dataset: Path) -> Path:
    return labelled_dataset / "files"


@pytest.fixture(scope="session")
def labels(labelled_dataset: Path) -> dict:
    import json

    return json.loads((labelled_dataset / "labels.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
