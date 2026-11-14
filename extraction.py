"""Invoice information extraction.

Two extractors share one output contract:

1. Rule-based extractor (always available, deterministic). Label-driven patterns, locale-aware
   amount parsing, multi-format date parsing, table-aware line-item detection.
2. Optional LLM extractor (Anthropic Messages API) enabled when ANTHROPIC_API_KEY is set. Its
   output is GROUNDED: any value that does not literally appear in the source text is discarded,
   so the model cannot invent invoice numbers, names or amounts. Any failure falls back to rules.

Missing or uncertain fields are returned as None with a confidence score; they are never guessed.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .config import Settings, settings as default_settings

log = logging.getLogger(__name__)

FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
NUMERIC_FIELDS = {"subtotal", "tax", "total"}
KNOWN_CURRENCIES = {"USD", "EUR", "GBP", "BDT", "INR", "JPY", "CAD", "AUD", "SGD", "AED", "CNY", "CHF", "SAR", "MYR", "PKR"}
SYMBOL_CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP", "৳": "BDT", "₹": "INR", "¥": "JPY"}
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
                "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d/%m/%y", "%m/%d/%y"]


@dataclass
class ExtractionResult:
    fields: dict[str, Any]
    line_items: list[dict[str, Any]]
    confidence: dict[str, float]
    raw_values: dict[str, str]  # present-but-unparseable source strings, used for INVALID_NUMBER checks
    method: str
    warnings: list[str] = field(default_factory=list)


def empty_fields() -> dict[str, Any]:
    return {f: None for f in FIELDS}


# --------------------------------------------------------------------------- parsing helpers

_MONEY_TOKEN = re.compile(r"-?\d[\d,.\s]*\d|-?\d")


def parse_amount(raw: str | None) -> float | None:
    """Parse '1,234.50', '1.234,50', '৳ 12,000', 'USD 99.9' etc. Returns None if no number."""
    if not raw:
        return None
    m = _MONEY_TOKEN.search(raw.replace(" ", " "))
    if not m:
        return None
    token = re.sub(r"\s", "", m.group(0))
    negative = token.startswith("-")
    token = token.lstrip("-")
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):  # European: 1.234,50
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        tail = token.rpartition(",")[2]
        token = token.replace(",", "") if len(tail) == 3 else token.replace(",", ".")
    try:
        value = float(token)
    except ValueError:
        return None
    return round(-value if negative else value, 2)


def parse_date(raw: str | None) -> tuple[str | None, float]:
    """Return (ISO date, confidence). Ambiguous dd/mm vs mm/dd dates default to day-first with lower confidence."""
    if not raw:
        return None, 0.0
    text = raw.replace(",", " ").strip().rstrip(".")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        confidence = 0.95
        if fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y"):
            a, b = int(re.split(r"[/]", text)[0]), int(re.split(r"[/]", text)[1])
            if a <= 12 and b <= 12 and a != b:
                confidence = 0.6  # ambiguous: day-first assumed
        if parsed > date(date.today().year + 2, 12, 31) or parsed.year < 1990:
            return None, 0.0
        return parsed.isoformat(), confidence
    return None, 0.0


def detect_currency(text: str, near: str | None = None) -> tuple[str | None, float]:
    haystack = near or text
    code = re.search(r"\b(" + "|".join(KNOWN_CURRENCIES) + r")\b", haystack)
    if code:
        return code.group(1), 0.9
    if re.search(r"\b(Tk|BDT)\b", haystack):
        return "BDT", 0.9
    for symbol, iso in SYMBOL_CURRENCIES.items():
        if symbol in haystack:
            return iso, 0.6 if symbol == "$" else 0.8  # '$' is shared by many currencies
    code = re.search(r"\b(" + "|".join(KNOWN_CURRENCIES) + r")\b", text)
    if code:
        return code.group(1), 0.7
    return None, 0.0


def _label_value(lines: list[str], label_pattern: str) -> str | None:
    """Value after a label on the same line, or on the next non-empty line (e.g. 'Bill To:' blocks)."""
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:\-#]?\s*(?P<v>.*?)\s*$", re.IGNORECASE)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        value = m.group("v").strip(" :-\t|")
        if value:
            return value
        for nxt in lines[i + 1:i + 3]:
            if nxt.strip():
                return nxt.strip(" |")
    return None


_LABELS = {
    "invoice_number": r"invoice\s*(?:no\.?|number|num|#|id)|inv\.?\s*(?:no\.?|#|number)|bill\s*(?:no\.?|number)",
    "invoice_date": r"invoice\s*date|date\s*of\s*(?:issue|invoice)|issue\s*date|date",
    "supplier_name": r"supplier(?:\s*name)?|vendor(?:\s*name)?|seller|bill\s*from|sold\s*by|from",
    "customer_name": r"customer(?:\s*name)?|client(?:\s*name)?|buyer|bill\s*to|sold\s*to|ship\s*to",
    "subtotal": r"sub\s*-?\s*total|net\s*amount|amount\s*before\s*tax",
    "tax": r"(?:tax|vat|gst)(?!\s*(?:id|no\.?|number|reg|registration))(?:\s*amount)?",
    "total": r"(?:grand\s*total|amount\s*due|balance\s*due|total\s*(?:due|payable|amount))|total(?!\s*(?:tax|vat|gst|quantity|qty))",
}


_RATE_NOTE = re.compile(r"\(\s*[\d.]+\s*%\s*\)")  # 'VAT (15%): 150.00' -> the 15 is a rate, not an amount


def _find_numeric(lines: list[str], label: str) -> tuple[float | None, str | None]:
    raw = _label_value(lines, _LABELS[label])
    if raw is None:
        return None, None
    return parse_amount(_RATE_NOTE.sub("", raw)), raw


def _find_invoice_number(lines: list[str]) -> str | None:
    raw = _label_value(lines, _LABELS["invoice_number"])
    if not raw:
        return None
    words = raw.split()
    token = words[0].strip(".:,") if words else ""
    if not re.search(r"\d", token) or len(token) > 40:
        return None
    return token


def _find_date(lines: list[str]) -> tuple[str | None, float]:
    raw = _label_value(lines, _LABELS["invoice_date"])
    if not raw:
        return None, 0.0
    date_like = re.search(
        r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}-[A-Za-z]{3}-\d{4}",
        raw,
    )
    return parse_date(date_like.group(0) if date_like else raw)


# --------------------------------------------------------------------------- line items

_ITEM_LINE = re.compile(
    r"^(?P<desc>[A-Za-z][^\n]{1,80}?)\s+(?P<qty>\d+(?:\.\d+)?)\s+(?P<unit>-?[\d,]+(?:\.\d+)?)\s+(?P<amount>-?[\d,]+(?:\.\d+)?)\s*$"
)
_NON_ITEM = re.compile(r"\b(total|subtotal|tax|vat|gst|amount|invoice|date|balance|due|description|item)\b", re.I)


def _items_from_text(lines: list[str]) -> list[dict[str, Any]]:
    items = []
    for line in lines:
        m = _ITEM_LINE.match(line.strip())
        if not m or _NON_ITEM.search(m.group("desc")):
            continue
        items.append({
            "description": m.group("desc").strip(),
            "quantity": parse_amount(m.group("qty")),
            "unit_price": parse_amount(m.group("unit")),
            "amount": parse_amount(m.group("amount")),
        })
    return items


_HEADER_ROLES = {
    "description": r"description|item|product|particulars?|service",
    "quantity": r"qty|quantity",
    "unit_price": r"unit\s*price|rate|unit\s*cost|price",
    "amount": r"amount|line\s*total|total|value",
}


def _items_from_tables(tables: list[list[list[str]]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table in tables:
        for hi, header in enumerate(table):
            roles: dict[str, int] = {}
            for ci, cell in enumerate(header):
                for role, pat in _HEADER_ROLES.items():
                    if role not in roles and re.fullmatch(rf"(?:{pat})\W*", cell.strip(), re.I):
                        roles[role] = ci
            if "description" in roles and "amount" in roles:
                for row in table[hi + 1:]:
                    desc = row[roles["description"]] if roles["description"] < len(row) else ""
                    if not desc or _NON_ITEM.search(desc):
                        if desc and re.search(r"total|subtotal", desc, re.I):
                            break
                        continue
                    get = lambda r: parse_amount(row[roles[r]]) if r in roles and roles[r] < len(row) else None
                    amount = get("amount")
                    if amount is None:
                        continue
                    items.append({"description": desc, "quantity": get("quantity"),
                                  "unit_price": get("unit_price"), "amount": amount})
                break
            if items:
                return items
    return items


# --------------------------------------------------------------------------- rule-based

def extract_rules(text: str, tables: list[list[list[str]]] | None = None, ocr: bool = False) -> ExtractionResult:
    lines = [ln for ln in text.replace("\f", "\n").splitlines()]
    fields = empty_fields()
    conf: dict[str, float] = {}
    raw_values: dict[str, str] = {}
    warnings: list[str] = []
    ocr_factor = 0.85 if ocr else 1.0

    def put(name: str, value: Any, confidence: float) -> None:
        if value is not None and value != "":
            fields[name] = value
            conf[name] = round(confidence * ocr_factor, 3)

    number = _find_invoice_number(lines)
    put("invoice_number", number, 0.9)

    inv_date, dconf = _find_date(lines)
    put("invoice_date", inv_date, dconf)
    if inv_date and dconf < 0.8:
        warnings.append("ambiguous_date_format: day/month order assumed")

    for name in ("supplier_name", "customer_name"):
        value = _label_value(lines, _LABELS[name])
        if value and not re.search(r"\d{5,}", value):  # reject values that are clearly numbers/IDs
            put(name, value.strip(" .,"), 0.9)  # explicit label match: strong evidence

    for name in NUMERIC_FIELDS:
        value, raw = _find_numeric(lines, name)
        if value is not None:
            put(name, value, 0.9)
        elif raw is not None:
            raw_values[name] = raw

    total_text = None
    for ln in lines:
        if re.search(_LABELS["total"], ln, re.I):
            total_text = ln
    currency, cconf = detect_currency(text, near=total_text)
    put("currency", currency, cconf)

    items = _items_from_tables(tables or []) or _items_from_text(lines)
    return ExtractionResult(fields=fields, line_items=items, confidence=conf,
                            raw_values=raw_values, method="rules", warnings=warnings)


# --------------------------------------------------------------------------- LLM (grounded)

LLM_SYSTEM = (
    "You extract invoice data. Reply with ONE JSON object and nothing else. Keys: invoice_number, "
    "invoice_date (YYYY-MM-DD), supplier_name, customer_name, subtotal, tax, total (numbers, no "
    "thousands separators), currency (ISO 4217), line_items (array of {description, quantity, "
    "unit_price, amount}). Use null for anything not explicitly present. Never guess or compute."
)


def _normalise(s: str) -> str:
    return re.sub(r"[\s,]+", " ", s).strip().lower()


def _grounded(value: Any, source: str) -> bool:
    """A value is grounded only if its literal form occurs in the source text."""
    if value is None:
        return True
    src = _normalise(source)
    if isinstance(value, (int, float)):
        forms = {f"{value:.2f}", f"{value:,.2f}", str(value), f"{value:,.0f}", f"{value:.0f}"}
        return any(_normalise(f) in src for f in forms)
    return _normalise(str(value)) in src


def extract_llm(text: str, cfg: Settings = default_settings, client: Any = None) -> dict[str, Any]:
    """Call the Messages API and return the raw parsed JSON. Raises on any transport/parse failure."""
    import httpx

    if not cfg.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    payload = {
        "model": cfg.llm_model,
        "max_tokens": 1500,
        "system": LLM_SYSTEM,
        "messages": [{"role": "user", "content": f"Invoice text:\n<<<\n{text[:30000]}\n>>>"}],
    }
    headers = {"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    http = client or httpx.Client(timeout=cfg.llm_timeout_s)
    resp = http.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    content = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("LLM response contained no JSON object")
    return json.loads(match.group(0))


def merge_llm(rules: ExtractionResult, llm: dict[str, Any], source: str) -> ExtractionResult:
    """Prefer grounded LLM values; keep rule values where the LLM returned null or ungrounded output."""
    warnings = list(rules.warnings)
    fields = dict(rules.fields)
    conf = dict(rules.confidence)
    for name in FIELDS:
        value = llm.get(name)
        if value in (None, ""):
            continue
        if name in NUMERIC_FIELDS:
            value = float(value) if isinstance(value, (int, float)) else parse_amount(str(value))
        if value is None or not _grounded(value, source):
            warnings.append(f"llm_value_discarded:{name} (not found in document text)")
            continue
        if name == "currency" and str(value).upper() not in KNOWN_CURRENCIES:
            continue
        if name == "currency":
            value = str(value).upper()
        fields[name] = value
        conf[name] = max(conf.get(name, 0.0), 0.85)
    items = []
    for item in llm.get("line_items") or []:
        amt = item.get("amount")
        if isinstance(amt, (int, float)) and _grounded(amt, source):
            items.append({"description": str(item.get("description") or "")[:200],
                          "quantity": item.get("quantity"), "unit_price": item.get("unit_price"),
                          "amount": float(amt)})
    return ExtractionResult(fields=fields, line_items=items or rules.line_items, confidence=conf,
                            raw_values=rules.raw_values, method="llm", warnings=warnings)


def extract_invoice(text: str, tables: list[list[list[str]]] | None = None, ocr: bool = False,
                    cfg: Settings = default_settings, client: Any = None) -> ExtractionResult:
    rules = extract_rules(text, tables, ocr=ocr)
    if not cfg.anthropic_api_key:
        return rules
    try:
        llm = extract_llm(text, cfg, client=client)
        return merge_llm(rules, llm, text)
    except Exception as exc:  # network, quota, malformed JSON: degrade gracefully to rules
        log.warning("LLM extraction failed, using rules: %s", exc)
        rules.warnings.append(f"llm_unavailable_fallback_to_rules: {exc.__class__.__name__}")
        return rules
