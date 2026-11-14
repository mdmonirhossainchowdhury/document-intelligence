"""Spreadsheet and CSV parsing (XLSX, XLS, CSV) with multi-sheet support.

Each sheet is read as raw cells (no header guessing by pandas), empty rows/columns are removed,
and two representations are produced:
  * `tables`: the cell grid, used to locate line-item tables;
  * `text`:   "Label: value" lines for two-column label/value rows plus pipe-joined rows,
              so the same extractor that works on PDFs also works on spreadsheets.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .errors import DocumentError

MAX_ROWS_PER_SHEET = 20_000
MAX_SHEETS = 20


@dataclass
class SheetResult:
    text: str
    tables: list[list[list[str]]]
    warnings: list[str] = field(default_factory=list)


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _grid_to_table(df: pd.DataFrame) -> list[list[str]]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    rows = [[_cell(v) for v in row] for row in df.itertuples(index=False, name=None)]
    return [r for r in rows if any(r)]


def _rows_to_text(rows: list[list[str]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        cells = [c for c in row if c]
        if not cells:
            continue
        if len(cells) == 2 and not cells[0].replace(".", "").isdigit():
            lines.append(f"{cells[0].rstrip(':')}: {cells[1]}")
        lines.append(" | ".join(cells))
    return lines


def _read_csv(path: Path) -> pd.DataFrame:
    """Read a CSV whose rows may have different widths (key/value header + line-item table).

    pandas would infer the column count from the first line and drop wider rows, so rows are read
    with the csv module and padded to the widest row instead.
    """
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DocumentError("corrupt_file", "CSV encoding could not be determined")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    width = max((len(r) for r in rows), default=0)
    padded = [r + [""] * (width - len(r)) for r in rows]
    return pd.DataFrame(padded, dtype=object)


def extract(path: Path, kind: str) -> SheetResult:
    warnings: list[str] = []
    try:
        if kind == "csv":
            sheets = {"csv": _read_csv(path)}
        elif kind == "xlsx":
            sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine="openpyxl")
        else:
            sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine="xlrd")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("corrupt_file", f"Spreadsheet could not be parsed: {exc.__class__.__name__}") from exc

    if len(sheets) > MAX_SHEETS:
        warnings.append(f"only first {MAX_SHEETS} of {len(sheets)} sheets processed")
        sheets = dict(list(sheets.items())[:MAX_SHEETS])

    tables, text_parts = [], []
    for name, df in sheets.items():
        if len(df) > MAX_ROWS_PER_SHEET:
            warnings.append(f"sheet '{name}' truncated to {MAX_ROWS_PER_SHEET} rows")
            df = df.head(MAX_ROWS_PER_SHEET)
        rows = _grid_to_table(df)
        if not rows:
            warnings.append(f"sheet '{name}' is empty")
            continue
        tables.append(rows)
        text_parts.append(f"[Sheet: {name}]")
        text_parts.extend(_rows_to_text(rows))

    text = "\n".join(text_parts)
    if not text.strip():
        raise DocumentError("no_text_found", "Spreadsheet contains no data")
    return SheetResult(text=text, tables=tables, warnings=warnings)
