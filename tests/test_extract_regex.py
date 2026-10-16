"""Corroboration-stream tests (S3-regex) against the real 17-PDF batch.

Assertions deliberately focus on the witness's OWN job: labels, formats, naive reads.
Arithmetic authority (subtotal + VAT) is what must equal the known oracle totals — a naive
read of a page-wrapped total is expected to be wrong and is verified as such.
"""
from decimal import Decimal

import pytest

from src.extract_regex import extract_invoice_regex, parse_money, read_pdf_text

# filename fragment -> (invoice_number, format, currency, expected total via arithmetic)
ORACLE = [
    ("03701", "INV-2025-03701", "A", "EUR", "8551.67"),
    ("03712", "INV-2025-03712", "A", "EUR", "6799.99"),
    ("03719", "INV-2025-03719", "A", "EUR", "5400.01"),
    ("03731", "INV-2025-03731", "A", "GBP", "4998.00"),
    ("03744", "INV-2025-03744", "A", "EUR", "3199.99"),
    ("03752", "INV-2025-03752", "A", "EUR", "7499.99"),
    ("03760", "INV-2025-03760", "A", "EUR", "10010.46"),
    ("03771", "INV-2025-03771", "A", "EUR", "4512.02"),
    ("03779", "INV-2025-03779", "A", "EUR", "2900.01"),
    ("03788", "INV-2025-03788", "A", "EUR", "42121.24"),
    ("03791", "INV-2025-03791", "A", "EUR", "8200.00"),
    ("03847", "INV-2025-03847", "A", "EUR", "5788.11"),
    ("03880", "INV-2025-03880", "A", "EUR", "11000.59"),
    ("03901", "INV-2025-03901", "A", "EUR", "18499.50"),
    ("03955", "INV-2025-03955", "A", "EUR", "6929.94"),
    ("SCAN-001", "INV-2025-SCAN-001", "B", "EUR", "1606.50"),
    ("SCAN-002", "INV-2025-SCAN-002", "B", "EUR", "2058.70"),
]

WRAPPED = {"03760", "03788", "03880", "03901"}          # total printed across multiple lines
NAIVE_WRONG = {"03760", "03788", "03880"}               # 03901's trailing '0' is numerically benign


def _extract(invoices_dir, fragment):
    path = next(p for p in invoices_dir.glob("*.pdf") if fragment in p.name)
    return extract_invoice_regex(read_pdf_text(path), path.name)


@pytest.mark.parametrize("fragment,inv,fmt,currency,total", ORACLE, ids=[o[0] for o in ORACLE])
def test_labels_formats_and_arithmetic_authority(invoices_dir, fragment, inv, fmt, currency, total):
    rec = _extract(invoices_dir, fragment)
    assert rec["invoice_number"] == inv
    assert rec["format_tag"] == fmt
    assert rec["currency"] == currency
    assert rec["subtotal"] is not None and rec["vat_amount"] is not None
    assert (rec["subtotal"] + rec["vat_amount"]).quantize(Decimal("0.01")) == Decimal(total)


@pytest.mark.parametrize("fragment", sorted(WRAPPED))
def test_wrapped_total_lines_are_captured_as_multiple_tokens(invoices_dir, fragment):
    """The wrap trap: the total is printed across lines and only joins to the authority."""
    rec = _extract(invoices_dir, fragment)
    authority = (rec["subtotal"] + rec["vat_amount"]).quantize(Decimal("0.01"))
    assert len(rec["total_raw"]["tokens"]) >= 2
    assert parse_money("".join(rec["total_raw"]["tokens"])) == authority


@pytest.mark.parametrize("fragment", sorted(NAIVE_WRONG))
def test_naive_witness_read_is_wrong_for_these_wraps(invoices_dir, fragment):
    rec = _extract(invoices_dir, fragment)
    authority = (rec["subtotal"] + rec["vat_amount"]).quantize(Decimal("0.01"))
    assert rec["total"] != authority


def test_clean_total_is_a_single_token(invoices_dir):
    rec = _extract(invoices_dir, "03701")
    assert len(rec["total_raw"]["tokens"]) == 1
    assert rec["total"] == Decimal("8551.67")


def test_sparkit_missing_po(invoices_dir):
    rec = _extract(invoices_dir, "03752")
    assert rec["po_number"] is None
    assert "po_number_absent" in rec["warnings"]


def test_quickfix_unicode_vendor_name(invoices_dir):
    rec = _extract(invoices_dir, "SCAN-001")
    assert rec["vendor_name"].startswith("QuickFix")
    assert rec["vendor_country"] == "Estonia"


def test_totalroute_free_text_note(invoices_dir):
    rec = _extract(invoices_dir, "03847")
    assert rec["free_text_note"] and "Re-submission" in rec["free_text_note"]


def test_logitrans_unknown_vendor_still_extracts(invoices_dir):
    rec = _extract(invoices_dir, "03955")
    assert rec["vendor_name"] == "LogiTrans Express SRL"
    assert rec["vendor_country"] == "Romania"
