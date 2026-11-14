"""Labelled synthetic invoice dataset generator.

Produces real files (PDF, scanned PDF, PNG, XLSX, CSV, corrupt files) plus a `labels.json` holding
the ground truth for every field, the expected line items and whether the record should be
flagged for manual review. Used by both the test-suite fixtures and `eval/run_eval.py`.

Important: this is SYNTHETIC data rendered from templates. Metrics measured on it show how the
system behaves on the template styles included here; they are not a substitute for measuring on
real, customer-provided invoices (see eval/README section of docs/EVALUATION.md).
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
SUPPLIERS = ["Apex Office Supplies Ltd", "Northwind Traders", "Bengal Tech Solutions", "Pinecrest Logistics",
             "Delta Print House", "Summit Cloud Services", "Riverside Stationery", "Orion Hardware Co"]
CUSTOMERS = ["Greenleaf Foods", "Blue Harbor Clinic", "Sunrise School Trust", "Metro Retail Group",
             "Karim & Sons", "Atlas Engineering", "Lotus Garments"]
ITEMS = ["Web hosting", "Consulting services", "Laptop stand", "Software license", "Support retainer",
         "Training workshop", "Cloud storage", "Printer cartridge", "Network cable pack", "Design work"]
NUMBER_LABELS = ["Invoice No:", "Invoice #", "Invoice Number:", "Inv. No:", "Bill No:"]
DATE_LABELS = ["Invoice Date:", "Date:", "Date of Issue:"]
SUPPLIER_LABELS = ["Supplier:", "Vendor:", "From:", "Seller:"]
CUSTOMER_LABELS = ["Customer:", "Bill To:", "Client:", "Buyer:"]
TOTAL_LABELS = ["Total:", "Grand Total:", "Amount Due:", "Total Due:"]
CURRENCIES = [("USD", "USD"), ("USD", "$"), ("BDT", "BDT"), ("BDT", "Tk"), ("EUR", "EUR"), ("GBP", "GBP")]


@dataclass
class Invoice:
    number: str
    issued: date
    supplier: str
    customer: str | None
    currency: str
    currency_token: str
    items: list[tuple[str, int, float]]
    tax_rate: float | None
    number_label: str
    date_label: str
    date_style: int
    total_label: str
    amount_comma: bool = True
    supplier_label: str = "Supplier:"
    customer_label: str = "Customer:"

    @property
    def subtotal(self) -> float:
        return round(sum(q * p for _, q, p in self.items), 2)

    @property
    def tax(self) -> float | None:
        return round(self.subtotal * self.tax_rate / 100, 2) if self.tax_rate is not None else None

    @property
    def total(self) -> float:
        return round(self.subtotal + (self.tax or 0.0), 2)

    def fmt_date(self) -> str:
        d = self.issued
        styles = [d.isoformat(), d.strftime("%d/%m/%Y"), f"{d.day} {d.strftime('%b')} {d.year}",
                  f"{d.strftime('%B')} {d.day}, {d.year}", d.strftime("%m/%d/%Y")]
        return styles[self.date_style % len(styles)]

    def fmt_amount(self, value: float) -> str:
        return f"{value:,.2f}" if self.amount_comma else f"{value:.2f}"

    def lines(self, tax_override: float | None = None, total_override: float | None = None) -> list[str]:
        out = ["INVOICE", f"{self.number_label} {self.number}", f"{self.date_label} {self.fmt_date()}",
               f"{self.supplier_label} {self.supplier}"]
        if self.customer:
            if self.customer_label == "Bill To:":
                out += ["Bill To:", self.customer]
            else:
                out.append(f"{self.customer_label} {self.customer}")
        out += ["", "Description Qty Unit Price Amount"]
        for desc, qty, price in self.items:
            out.append(f"{desc} {qty} {self.fmt_amount(price)} {self.fmt_amount(qty * price)}")
        out.append(f"Subtotal: {self.fmt_amount(self.subtotal)}")
        if self.tax_rate is not None:
            tax = tax_override if tax_override is not None else self.tax
            out.append(f"Tax ({self.tax_rate:g}%): {self.fmt_amount(tax)}")
        total = total_override if total_override is not None else self.total
        token = self.currency_token if self.currency_token not in ("$",) else "$"
        if token == self.currency:
            prefix = f"{self.currency} "
        elif token == "$":
            prefix = "$"
        else:
            prefix = f"{token} "
        out.append(f"{self.total_label} {prefix}{self.fmt_amount(total)}")
        return out

    def expected(self) -> dict:
        return {
            "invoice_number": self.number,
            "invoice_date": self.issued.isoformat(),
            "supplier_name": self.supplier,
            "customer_name": self.customer,
            "subtotal": self.subtotal,
            "tax": self.tax,
            "total": self.total,
            "currency": self.currency,
            "line_items": [{"description": d, "quantity": q, "unit_price": p, "amount": round(q * p, 2)}
                           for d, q, p in self.items],
        }


@dataclass
class Sample:
    id: str
    path: str  # relative to dataset dir
    category: str
    expected: dict
    expected_review: bool
    notes: str = ""
    corrupt: bool = False
    tags: list[str] = field(default_factory=list)


def _random_invoice(rng: random.Random, number: str | None = None, base: date | None = None) -> Invoice:
    currency, token = rng.choice(CURRENCIES)
    n_items = rng.randint(1, 4)
    items = [(rng.choice(ITEMS), rng.randint(1, 9), round(rng.uniform(5, 900), 2)) for _ in range(n_items)]
    issued = (base or date(2024, 1, 1)) + timedelta(days=rng.randint(0, 600))
    if issued.day <= 12:  # keep day > 12 so slash dates are unambiguous in the ground truth
        issued = issued.replace(day=issued.day + 12) if issued.day + 12 <= 28 else issued.replace(day=20)
    return Invoice(
        number=number or f"INV-{rng.randint(2024, 2026)}-{rng.randint(1000, 9999)}",
        issued=issued,
        supplier=rng.choice(SUPPLIERS),
        customer=rng.choice(CUSTOMERS),
        currency=currency,
        currency_token=token,
        items=items,
        tax_rate=rng.choice([5.0, 10.0, 15.0, None]),
        number_label=rng.choice(NUMBER_LABELS),
        date_label=rng.choice(DATE_LABELS),
        date_style=rng.randrange(5),
        total_label=rng.choice(TOTAL_LABELS),
        amount_comma=rng.random() < 0.7,
        supplier_label=rng.choice(SUPPLIER_LABELS),
        customer_label=rng.choice(CUSTOMER_LABELS),
    )


# --------------------------------------------------------------------------- renderers

def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def write_digital_pdf(path: Path, lines: list[str], extra_pages: int = 0) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in lines:
        c.setFont("Helvetica-Bold" if line in ("INVOICE",) else "Helvetica", 11)
        c.drawString(60, y, line)
        y -= 18
    c.showPage()
    for n in range(extra_pages):
        c.setFont("Helvetica", 10)
        c.drawString(60, 800, f"Terms and conditions (page {n + 2})")
        c.drawString(60, 780, "Payment is due within 30 days. Late payments incur a 2% monthly charge.")
        c.showPage()
    c.save()


def render_scan(lines: list[str], degrade: bool, seed: int) -> Image.Image:
    width, height = 1240, 1754  # A4 at ~150 dpi
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    font = _font(24)
    y = 110
    for line in lines:
        draw.text((90, y), line, fill=0, font=font)
        y += 46
    if degrade:
        rng = random.Random(seed)
        img = img.filter(ImageFilter.GaussianBlur(radius=1.4))
        px = img.load()
        for _ in range(width * height // 60):
            x, yy = rng.randrange(width), rng.randrange(height)
            px[x, yy] = rng.randrange(120, 255)
        img = img.rotate(0.8, fillcolor=255, expand=False)
    return img.convert("RGB")


def write_scanned_pdf(path: Path, image: Image.Image) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()


def write_xlsx(path: Path, inv: Invoice) -> None:
    import pandas as pd

    summary_rows = [
        ["Invoice Number", inv.number],
        ["Invoice Date", inv.fmt_date()],
        ["Supplier", inv.supplier],
    ]
    if inv.customer:
        summary_rows.append(["Customer", inv.customer])
    summary_rows += [["Subtotal", inv.subtotal]]
    if inv.tax_rate is not None:
        summary_rows.append(["Tax", inv.tax])
    summary_rows += [["Total", inv.total], ["Currency", inv.currency]]
    items = [["Description", "Qty", "Unit Price", "Amount"]]
    items += [[d, q, p, round(q * p, 2)] for d, q, p in inv.items]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Invoice", header=False, index=False)
        pd.DataFrame(items).to_excel(writer, sheet_name="Line Items", header=False, index=False)


def write_csv(path: Path, inv: Invoice) -> None:
    import csv as csv_mod

    rows = [["Invoice Number", inv.number], ["Invoice Date", inv.fmt_date()], ["Supplier", inv.supplier]]
    if inv.customer:
        rows.append(["Customer", inv.customer])
    rows.append(["Subtotal", f"{inv.subtotal:.2f}"])
    if inv.tax_rate is not None:
        rows.append(["Tax", f"{inv.tax:.2f}"])
    rows += [["Total", f"{inv.total:.2f}"], ["Currency", inv.currency], [], ["Description", "Qty", "Unit Price", "Amount"]]
    rows += [[d, str(q), f"{p:.2f}", f"{q * p:.2f}"] for d, q, p in inv.items]
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv_mod.writer(fh).writerows(rows)  # quoting handles commas inside values, e.g. "May 16, 2025"


# --------------------------------------------------------------------------- dataset

def build(out_dir: Path, seed: int = 7) -> dict:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []

    def add(category: str, name: str, inv: Invoice | None, expected: dict | None, review: bool,
            notes: str = "", corrupt: bool = False, tags: list[str] | None = None) -> None:
        samples.append(Sample(id=name, path=f"files/{name}", category=category,
                              expected=expected or {}, expected_review=review, notes=notes,
                              corrupt=corrupt, tags=tags or []))

    files = out_dir / "files"
    files.mkdir(exist_ok=True)

    # 1. Digital PDFs (some multi-page, some with missing optional fields)
    for i in range(20):
        inv = _random_invoice(rng)
        missing_customer = i % 6 == 0
        missing_tax = i % 7 == 0
        if missing_customer:
            inv.customer = None
        if missing_tax:
            inv.tax_rate = None
        name = f"digital_{i:02d}.pdf"
        write_digital_pdf(files / name, inv.lines(), extra_pages=1 if i % 4 == 0 else 0)
        expected = inv.expected()
        if missing_customer:
            expected["customer_name"] = None
        if missing_tax:
            expected["tax"] = None
        add("digital_pdf", name, inv, expected, review=False,
            notes="multi-page" if i % 4 == 0 else "",
            tags=["missing_optional"] if (missing_customer or missing_tax) else [])

    # 2. Scanned PDFs (raster only, OCR path)
    for i in range(6):
        inv = _random_invoice(rng)
        name = f"scanned_pdf_{i:02d}.pdf"
        write_scanned_pdf(files / name, render_scan(inv.lines(), degrade=False, seed=i))
        add("scanned_pdf", name, inv, inv.expected(), review=False)

    # 3. Images (PNG): half clean, half degraded (blur, noise, skew)
    for i in range(12):
        inv = _random_invoice(rng)
        degrade = i >= 6
        name = f"image_{i:02d}{'_degraded' if degrade else ''}.png"
        render_scan(inv.lines(), degrade=degrade, seed=100 + i).save(files / name)
        add("image_degraded" if degrade else "image_clean", name, inv, inv.expected(), review=False)

    # 4. XLSX (two sheets) and CSV
    for i in range(10):
        inv = _random_invoice(rng)
        name = f"sheet_{i:02d}.xlsx"
        write_xlsx(files / name, inv)
        add("xlsx", name, inv, inv.expected(), review=False)
    for i in range(8):
        inv = _random_invoice(rng)
        name = f"sheet_{i:02d}.csv"
        write_csv(files / name, inv)
        add("csv", name, inv, inv.expected(), review=False)

    # 5. Cross-verification set: same invoice as PDF and as XLSX (values agree)
    for i in range(3):
        inv = _random_invoice(rng, number=f"XV-{2025}-{100 + i}")
        pdf_name, xls_name = f"xverify_{i}_pdf.pdf", f"xverify_{i}_sheet.xlsx"
        write_digital_pdf(files / pdf_name, inv.lines())
        write_xlsx(files / xls_name, inv)
        add("cross_verify", pdf_name, inv, inv.expected(), review=False, tags=["pair_match"])
        add("cross_verify", xls_name, inv, inv.expected(), review=False, tags=["pair_match"])

    # 6. Arithmetic error: printed total does not equal subtotal + tax (must be flagged)
    for i in range(3):
        inv = _random_invoice(rng)
        inv.tax_rate = 10.0
        wrong_total = round(inv.total + 37.5, 2)
        name = f"bad_total_{i}.pdf"
        write_digital_pdf(files / name, inv.lines(total_override=wrong_total))
        expected = inv.expected()
        expected["total"] = wrong_total  # ground truth is what is printed on the page
        add("arithmetic_error", name, inv, expected, review=True, tags=["expect_total_mismatch"])

    # 7. Duplicate invoice (same number and supplier, uploaded twice as PDFs)
    dup = _random_invoice(rng, number="DUP-2025-777")
    write_digital_pdf(files / "duplicate_a.pdf", dup.lines())
    write_digital_pdf(files / "duplicate_b.pdf", dup.lines())
    add("duplicate", "duplicate_a.pdf", dup, dup.expected(), review=True, tags=["expect_duplicate"])
    add("duplicate", "duplicate_b.pdf", dup, dup.expected(), review=True, tags=["expect_duplicate"])

    # 8. Missing supplier (required field) -> must be flagged, supplier must be null (not invented)
    inv = _random_invoice(rng)
    inv.supplier_label = "Notes:"  # supplier not labelled in a recognised way
    inv.supplier = "Hidden Supplier Name"
    lines = [ln for ln in inv.lines() if not ln.startswith("Notes:")]
    name = "missing_supplier.pdf"
    write_digital_pdf(files / name, lines)
    expected = inv.expected()
    expected["supplier_name"] = None
    add("missing_required", name, inv, expected, review=True, tags=["expect_missing_required"])

    # 9. Corrupt / unreadable files (must FAIL gracefully, never crash the worker)
    (files / "corrupt_random.pdf").write_bytes(b"%PDF-1.4\n" + bytes(random.Random(1).randrange(256) for _ in range(4000)))
    add("corrupt", "corrupt_random.pdf", None, {}, review=True, corrupt=True)
    good = (files / "digital_00.pdf").read_bytes()
    (files / "truncated.pdf").write_bytes(good[: len(good) // 3])
    add("corrupt", "truncated.pdf", None, {}, review=True, corrupt=True)
    (files / "wrong_extension.png").write_text("this is a plain text file, not an image\n")
    add("corrupt", "wrong_extension.png", None, {}, review=True, corrupt=True)
    (files / "garbage.xlsx").write_bytes(b"\x00\x01\x02not a zip archive\xff" * 50)
    add("corrupt", "garbage.xlsx", None, {}, review=True, corrupt=True)

    labels = {"version": 1, "seed": seed, "samples": [asdict(s) for s in samples]}
    (out_dir / "labels.json").write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
    return labels


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "dataset"
    data = build(target)
    print(f"generated {len(data['samples'])} labelled samples in {target}")
