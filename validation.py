"""Validation rules for extracted invoice records.

Pure functions: the caller supplies the record, line items, and the *other* documents that share
an invoice number, so every rule is unit-testable without a database.

Severity model
--------------
* error   -> record is flagged `needs_review` (a human must confirm before it is used)
* warning -> shown to the reviewer but does not block the record
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .extraction import KNOWN_CURRENCIES

TOLERANCE = 0.01
REQUIRED_FIELDS = ["invoice_number", "invoice_date", "supplier_name", "total"]
RECOMMENDED_FIELDS = ["customer_name", "currency", "subtotal", "tax"]


@dataclass
class Issue:
    rule: str
    severity: str  # error | warning
    field: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity, "field": self.field, "message": self.message}


def normalise_number(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def normalise_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE


def validate(
    fields: dict[str, Any],
    line_items: list[dict[str, Any]],
    raw_values: dict[str, str] | None = None,
    field_confidence: dict[str, float] | None = None,
    processing_warnings: list[str] | None = None,
    other_documents: list[dict[str, Any]] | None = None,
    sha256_duplicates: list[str] | None = None,
    today: date | None = None,
    file_type: str | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    today = today or date.today()

    # 1. Missing fields
    for name in REQUIRED_FIELDS:
        if fields.get(name) in (None, ""):
            issues.append(Issue("MISSING_REQUIRED_FIELD", "error", name, f"Required field '{name}' is missing"))
    for name in RECOMMENDED_FIELDS:
        if fields.get(name) in (None, ""):
            issues.append(Issue("MISSING_FIELD", "warning", name, f"Field '{name}' was not found in the document"))

    # 2. Unparseable numeric values (e.g. 'TBD', 'N/A' in a total cell)
    for name, raw in (raw_values or {}).items():
        issues.append(Issue("INVALID_NUMBER", "error", name, f"'{name}' value '{raw}' is not a valid number"))

    # 3. Sign / magnitude
    for name in ("subtotal", "tax", "total"):
        value = fields.get(name)
        if value is not None and value < 0:
            issues.append(Issue("NEGATIVE_AMOUNT", "error", name, f"'{name}' is negative ({value})"))
    if fields.get("total") == 0:
        issues.append(Issue("ZERO_TOTAL", "warning", "total", "Total is zero; confirm this is intended"))

    # 4. Dates
    inv_date = fields.get("invoice_date")
    if inv_date:
        try:
            parsed = date.fromisoformat(inv_date)
            if parsed > today + timedelta(days=1):
                issues.append(Issue("FUTURE_DATE", "warning", "invoice_date", f"Invoice date {inv_date} is in the future"))
        except ValueError:
            issues.append(Issue("INVALID_DATE", "error", "invoice_date", f"Invoice date '{inv_date}' is not a valid date"))

    # 5. Currency
    currency = fields.get("currency")
    if currency and currency not in KNOWN_CURRENCIES:
        issues.append(Issue("UNKNOWN_CURRENCY", "warning", "currency", f"Currency '{currency}' is not a recognised ISO 4217 code"))

    # 6. Arithmetic: subtotal + tax = total
    subtotal, tax, total = fields.get("subtotal"), fields.get("tax"), fields.get("total")
    if subtotal is not None and total is not None:
        expected = round(subtotal + (tax or 0.0), 2)
        if not _close(expected, total):
            issues.append(Issue("TOTAL_MISMATCH", "error", "total",
                                f"subtotal ({subtotal:.2f}) + tax ({(tax or 0):.2f}) = {expected:.2f}, but total is {total:.2f}"))

    # 7. Line items: each row's qty x unit = amount, and sum(amount) = subtotal
    for idx, item in enumerate(line_items):
        qty, unit, amount = item.get("quantity"), item.get("unit_price"), item.get("amount")
        if None not in (qty, unit, amount) and not _close(round(qty * unit, 2), amount):
            issues.append(Issue("LINE_ITEM_ARITHMETIC", "warning", f"line_items[{idx}]",
                                f"Line {idx + 1}: {qty} x {unit:.2f} = {qty * unit:.2f}, but amount is {amount:.2f}"))
    if not line_items:
        issues.append(Issue("NO_LINE_ITEMS", "warning", "line_items", "No line items were detected"))
    elif subtotal is not None:
        items_sum = round(sum(i.get("amount") or 0.0 for i in line_items), 2)
        if not _close(items_sum, subtotal):
            issues.append(Issue("LINE_ITEMS_SUBTOTAL_MISMATCH", "error", "subtotal",
                                f"Line items sum to {items_sum:.2f}, but subtotal is {subtotal:.2f}"))

    # 8. Duplicates and cross-document verification
    # Same invoice number seen in another document. Decision table:
    #   values differ                          -> CROSS_DOCUMENT_MISMATCH (error, both records flagged)
    #   same supplier, same file type, agree   -> DUPLICATE_INVOICE (error: probable double entry)
    #   same supplier, other file type, agree  -> CROSS_DOCUMENT_MATCH (warning: PDF<->Excel verified)
    #   different supplier                     -> INVOICE_NUMBER_REUSED (warning)
    num = normalise_number(fields.get("invoice_number"))
    if num:
        for other in other_documents or []:
            if normalise_number(other.get("invoice_number")) != num:
                continue
            ref = other["id"][:8]
            differs = []
            if other.get("total") is not None and total is not None and not _close(other["total"], total):
                differs.append(f"total {other['total']:.2f} vs {total:.2f}")
            if other.get("invoice_date") and inv_date and other["invoice_date"] != inv_date:
                differs.append(f"date {other['invoice_date']} vs {inv_date}")
            same_supplier = bool(normalise_name(fields.get("supplier_name"))) and \
                normalise_name(other.get("supplier_name")) == normalise_name(fields.get("supplier_name"))
            if differs and same_supplier:
                issues.append(Issue("CROSS_DOCUMENT_MISMATCH", "error", "total",
                                    f"Document {ref} has the same invoice number but {'; '.join(differs)}"))
            elif not same_supplier:
                issues.append(Issue("INVOICE_NUMBER_REUSED", "warning", "invoice_number",
                                    f"Invoice number also appears on document {ref} from a different supplier"))
            elif other.get("file_type") != file_type:
                issues.append(Issue("CROSS_DOCUMENT_MATCH", "warning", "invoice_number",
                                    f"Verified: matches the {other.get('file_type')} counterpart (document {ref})"))
            else:
                issues.append(Issue("DUPLICATE_INVOICE", "error", "invoice_number",
                                    f"Invoice {fields['invoice_number']} from this supplier already exists (document {ref})"))

    for dup_id in sha256_duplicates or []:
        issues.append(Issue("DUPLICATE_FILE", "warning", None, f"Identical file content already uploaded (document {dup_id[:8]})"))

    # 9. Extraction confidence and OCR quality
    for name, conf in (field_confidence or {}).items():
        if conf < 0.7:
            issues.append(Issue("LOW_FIELD_CONFIDENCE", "warning", name, f"Low confidence ({conf:.2f}) for '{name}'"))
    for warning in processing_warnings or []:
        code = warning.split(":", 1)[0]
        if code == "very_low_ocr_confidence":
            issues.append(Issue("POOR_IMAGE_QUALITY", "error", None, warning))
        elif code in ("low_ocr_confidence", "ambiguous_date_format", "llm_unavailable_fallback_to_rules") or code.startswith("llm_value_discarded"):
            issues.append(Issue(code.upper(), "warning", None, warning))
        else:
            issues.append(Issue("PROCESSING_NOTICE", "warning", None, warning))

    return issues


def needs_review(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
