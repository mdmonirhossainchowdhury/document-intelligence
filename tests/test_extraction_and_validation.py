"""Unit tests for amount/date parsing, rule-based extraction, grounded LLM merging and validation rules."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.config import Settings
from app.extraction import (empty_fields, extract_invoice, extract_rules, parse_amount, parse_date,
                            detect_currency)
from app.validation import needs_review, validate

SAMPLE = """INVOICE
Invoice No: INV-2025-0042
Invoice Date: 15 Mar 2025
Supplier: Apex Office Supplies Ltd
Bill To:
Greenleaf Foods
Description Qty Unit Price Amount
Laptop stand 2 1,250.00 2,500.00
Cloud storage 3 100.00 300.00
Subtotal: 2,800.00
Tax (10%): 280.00
Total: USD 3,080.00
"""


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw,expected", [
    ("1,234.50", 1234.5), ("1234.5", 1234.5), ("1.234,50", 1234.5), ("12,5", 12.5),
    ("USD 99.90", 99.9), ("৳ 12,000", 12000.0), ("$1,234,567.00", 1234567.0), ("-40.00", -40.0),
    ("(15%)", 15.0),  # rate notes are stripped by the caller; parser just finds the number
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["N/A", "TBD", "", None])
def test_parse_amount_rejects_non_numbers(raw):
    assert parse_amount(raw) is None


@pytest.mark.parametrize("raw,iso", [
    ("2025-03-15", "2025-03-15"), ("15/03/2025", "2025-03-15"), ("15 Mar 2025", "2025-03-15"),
    ("March 15, 2025", "2025-03-15"), ("03/15/2025", "2025-03-15"), ("15-Mar-2025", "2025-03-15"),
])
def test_parse_date_formats(raw, iso):
    assert parse_date(raw)[0] == iso


def test_ambiguous_date_is_low_confidence():
    value, conf = parse_date("05/03/2025")
    assert value == "2025-03-05" and conf < 0.8


def test_unparseable_date_returns_none():
    assert parse_date("sometime in spring") == (None, 0.0)


def test_currency_detection_prefers_iso_code_near_total():
    assert detect_currency("Total: USD 10", near="Total: USD 10")[0] == "USD"
    assert detect_currency("Amount Due: Tk 500")[0] == "BDT"
    assert detect_currency("Total: € 500")[0] == "EUR"
    assert detect_currency("Total: $ 500")[0] == "USD"


# --------------------------------------------------------------------------- rule-based extraction

def test_rules_extract_all_core_fields():
    r = extract_rules(SAMPLE)
    assert r.fields["invoice_number"] == "INV-2025-0042"
    assert r.fields["invoice_date"] == "2025-03-15"
    assert r.fields["supplier_name"] == "Apex Office Supplies Ltd"
    assert r.fields["customer_name"] == "Greenleaf Foods"  # value on the line after 'Bill To:'
    assert r.fields["subtotal"] == 2800.0
    assert r.fields["tax"] == 280.0  # '(10%)' is a rate note, not the amount
    assert r.fields["total"] == 3080.0
    assert r.fields["currency"] == "USD"


def test_rules_extract_line_items_from_text():
    items = extract_rules(SAMPLE).line_items
    assert [i["description"] for i in items] == ["Laptop stand", "Cloud storage"]
    assert items[0] == {"description": "Laptop stand", "quantity": 2.0, "unit_price": 1250.0, "amount": 2500.0}


def test_missing_fields_are_null_never_invented():
    text = "Invoice No: AB-77\nTotal: 500.00\n"
    r = extract_rules(text)
    assert r.fields["invoice_number"] == "AB-77"
    assert r.fields["total"] == 500.0
    for name in ("supplier_name", "customer_name", "invoice_date", "subtotal", "tax"):
        assert r.fields[name] is None
    assert r.line_items == []


def test_unparseable_total_is_recorded_as_raw_value():
    r = extract_rules("Invoice No: X-1\nTotal: TBD\n")
    assert r.fields["total"] is None
    assert r.raw_values == {"total": "TBD"}


def test_table_based_line_items_from_spreadsheet_rows():
    tables = [[["Description", "Qty", "Unit Price", "Amount"],
               ["Widget", "3", "10", "30"],
               ["Gadget", "1", "5.5", "5.5"],
               ["Total", "", "", "35.5"]]]
    r = extract_rules("", tables)
    assert [i["description"] for i in r.line_items] == ["Widget", "Gadget"]
    assert r.line_items[1]["amount"] == 5.5


def test_ocr_flag_reduces_confidence():
    plain = extract_rules(SAMPLE).confidence["total"]
    ocr = extract_rules(SAMPLE, ocr=True).confidence["total"]
    assert ocr < plain


# --------------------------------------------------------------------------- LLM path (mocked, grounded)

def _cfg(**kw) -> Settings:
    base = dict(data_dir=None, anthropic_api_key="test-key", llm_model="claude-haiku-5-5", llm_timeout_s=5)
    base.update(kw)
    return Settings(**{k: v for k, v in base.items() if k != "data_dir"})


def _mock_anthropic(payload_json: str, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        body = {"content": [{"type": "text", "text": payload_json}]}
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_llm_values_are_grounded_and_hallucinations_discarded():
    llm = ('{"invoice_number": "INV-2025-0042", "supplier_name": "Acme Global Holdings", '
           '"total": 9999.99, "subtotal": 2800.0, "tax": 280.0, "currency": "USD", "line_items": []}')
    result = extract_invoice(SAMPLE, cfg=_cfg(), client=_mock_anthropic(llm))
    assert result.method == "llm"
    assert result.fields["invoice_number"] == "INV-2025-0042"
    assert result.fields["supplier_name"] == "Apex Office Supplies Ltd"  # hallucinated supplier discarded
    assert result.fields["total"] == 3080.0  # 9999.99 is not in the document -> rules value kept
    assert any("llm_value_discarded:total" in w for w in result.warnings)
    assert any("llm_value_discarded:supplier_name" in w for w in result.warnings)


def test_llm_failure_falls_back_to_rules():
    result = extract_invoice(SAMPLE, cfg=_cfg(), client=_mock_anthropic("not json at all"))
    assert result.method == "rules"
    assert result.fields["total"] == 3080.0
    assert any(w.startswith("llm_unavailable_fallback_to_rules") for w in result.warnings)


def test_llm_http_error_falls_back_to_rules():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})))
    result = extract_invoice(SAMPLE, cfg=_cfg(), client=client)
    assert result.method == "rules"


# --------------------------------------------------------------------------- validation rules

def _fields(**overrides):
    base = dict(empty_fields(), invoice_number="INV-1", invoice_date="2025-03-15", supplier_name="Acme",
                customer_name="Bob", subtotal=100.0, tax=10.0, total=110.0, currency="USD")
    base.update(overrides)
    return base


ITEMS = [{"description": "A", "quantity": 1, "unit_price": 100.0, "amount": 100.0}]


def _rules(issues):
    return {i.rule for i in issues}


def test_clean_record_has_no_errors():
    issues = validate(_fields(), ITEMS, today=date(2025, 4, 1))
    assert not needs_review(issues)


def test_missing_required_fields_are_errors():
    issues = validate(_fields(invoice_number=None, supplier_name=None, total=None), ITEMS, today=date(2025, 4, 1))
    missing = [i for i in issues if i.rule == "MISSING_REQUIRED_FIELD"]
    assert {i.field for i in missing} == {"invoice_number", "supplier_name", "total"}
    assert needs_review(issues)


def test_missing_optional_field_is_warning_only():
    issues = validate(_fields(customer_name=None), ITEMS, today=date(2025, 4, 1))
    assert "MISSING_FIELD" in _rules(issues)
    assert not needs_review(issues)


def test_total_mismatch_detected():
    issues = validate(_fields(total=120.0), ITEMS, today=date(2025, 4, 1))
    assert "TOTAL_MISMATCH" in _rules(issues) and needs_review(issues)


def test_line_items_must_sum_to_subtotal():
    items = [{"description": "A", "quantity": 1, "unit_price": 60.0, "amount": 60.0}]
    assert "LINE_ITEMS_SUBTOTAL_MISMATCH" in _rules(validate(_fields(), items, today=date(2025, 4, 1)))


def test_line_item_arithmetic_warning():
    items = [{"description": "A", "quantity": 2, "unit_price": 50.0, "amount": 90.0}]
    assert "LINE_ITEM_ARITHMETIC" in _rules(validate(_fields(), items, today=date(2025, 4, 1)))


def test_negative_and_invalid_numbers():
    issues = validate(_fields(tax=-5.0), ITEMS, raw_values={"total": "N/A"}, today=date(2025, 4, 1))
    assert "NEGATIVE_AMOUNT" in _rules(issues)
    assert "INVALID_NUMBER" in _rules(issues)


def test_future_date_warning():
    issues = validate(_fields(invoice_date="2030-01-01"), ITEMS, today=date(2025, 4, 1))
    assert "FUTURE_DATE" in _rules(issues)


def test_duplicate_invoice_same_supplier_is_error():
    other = [{"id": "abcdef12-0000", "invoice_number": "inv-1", "supplier_name": "ACME", "total": 110.0,
              "invoice_date": "2025-03-15"}]
    issues = validate(_fields(), ITEMS, other_documents=other, today=date(2025, 4, 1))
    assert "DUPLICATE_INVOICE" in _rules(issues) and needs_review(issues)


def test_pdf_vs_excel_value_mismatch_is_error():
    other = [{"id": "11111111-0000", "invoice_number": "INV-1", "supplier_name": "Acme", "total": 125.0,
              "invoice_date": "2025-03-15", "file_type": "spreadsheet"}]
    issues = validate(_fields(), ITEMS, other_documents=other, today=date(2025, 4, 1), file_type="pdf")
    assert "CROSS_DOCUMENT_MISMATCH" in _rules(issues) and needs_review(issues)


def test_pdf_vs_excel_agreement_is_verification_not_duplicate():
    other = [{"id": "11111111-0000", "invoice_number": "INV-1", "supplier_name": "Acme", "total": 110.0,
              "invoice_date": "2025-03-15", "file_type": "spreadsheet"}]
    issues = validate(_fields(), ITEMS, other_documents=other, today=date(2025, 4, 1), file_type="pdf")
    assert "CROSS_DOCUMENT_MATCH" in _rules(issues)
    assert "DUPLICATE_INVOICE" not in _rules(issues) and not needs_review(issues)


def test_same_number_different_supplier_is_warning_only():
    other = [{"id": "11111111-0000", "invoice_number": "INV-1", "supplier_name": "Other Co", "total": 110.0,
              "invoice_date": "2025-03-15", "file_type": "pdf"}]
    issues = validate(_fields(), ITEMS, other_documents=other, today=date(2025, 4, 1), file_type="pdf")
    assert "INVOICE_NUMBER_REUSED" in _rules(issues) and not needs_review(issues)


def test_low_field_confidence_and_poor_image_quality():
    issues = validate(_fields(), ITEMS, field_confidence={"total": 0.4},
                      processing_warnings=["very_low_ocr_confidence: image quality too poor"], today=date(2025, 4, 1))
    assert "LOW_FIELD_CONFIDENCE" in _rules(issues)
    assert "POOR_IMAGE_QUALITY" in _rules(issues) and needs_review(issues)
