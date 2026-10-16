"""Verification layer tests — pure, no LLM, no PDFs (arithmetic authority is the law here)."""
from decimal import Decimal

from src.validate_extract import verify


def _record(**kw):
    base = {
        "vendor_name": "EuroFuel GmbH",
        "vendor_vat": "DE123456789",
        "invoice_number": "INV-2025-03701",
        "invoice_date": "2025-06-05",
        "po_number": "PO-55210",
        "currency": "EUR",
        "subtotal": Decimal("7186.28"),
        "vat_amount": Decimal("1365.39"),
        "total": Decimal("8551.67"),
        "line_items": [
            {"description": "Diesel", "qty": 2291,
             "unit_price": Decimal("1.54"), "amount": Decimal("3528.14")},
        ],
    }
    base.update(kw)
    return base


def test_clean_total_agrees_authority():
    rec = verify(_record())
    v = rec["verification"]
    assert v["total_authority"] == Decimal("8551.67")
    assert v["total_agrees_with_ai"] is True
    assert rec["total_trusted"] == Decimal("8551.67")
    assert v["ok"] is True


def test_wrapped_total_ai_misread_authority_wins():
    # the AlpinaDiesel trap: the model reads '€4' from a page-wrapped TOTAL line
    rec = verify(
        _record(
            subtotal=Decimal("35396.00"), vat_amount=Decimal("6725.24"),
            total=Decimal("4"), invoice_number="INV-2025-03788",
            total_raw={"line_text": "TOTAL €4", "tokens": ["€4", "2,121.24"]},
        ),
        corroboration={  # the witness reads the same printed document
            "vendor_name": "AlpinaDiesel AG", "vendor_vat": "ATU12345678",
            "invoice_number": "INV-2025-03788", "invoice_date": "2025-06-18",
            "po_number": "PO-55290", "currency": "EUR",
            "subtotal": Decimal("35396.00"), "vat_amount": Decimal("6725.24"),
            "total": Decimal("4"),
        },
    )
    v = rec["verification"]
    assert v["total_agrees_with_ai"] is False          # the model's own total was wrong
    assert v["wrap_detected"] is True
    assert rec["total_trusted"] == Decimal("42121.24")  # authority + joined tokens, not the misread
    assert rec["total_witness_ai"] == Decimal("4")


def test_line_math_failure_detected():
    rec = verify(_record(line_items=[
        {"description": "Diesel", "qty": 100,
         "unit_price": Decimal("1.54"), "amount": Decimal("999.99")},
    ]))
    v = rec["verification"]
    assert v["line_math_ok"] is False
    assert v["line_math_failures"] == [0]
    assert v["ok"] is False


def test_missing_subtotal_is_a_hard_failure():
    rec = verify(_record(subtotal=None))
    v = rec["verification"]
    assert "subtotal_or_vat_unparsed" in v["failures"]
    assert rec["total_trusted"] is None


def test_agreement_scoring_both_null_counts_as_agree():
    corroboration = {
        "vendor_name": "EuroFuel GmbH", "vendor_vat": "DE123456789",
        "invoice_number": "INV-2025-03701", "invoice_date": "2025-06-05",
        "po_number": None, "currency": "EUR",
        "subtotal": Decimal("7186.28"), "vat_amount": Decimal("1365.39"),
        "total": Decimal("8551.67"),
    }
    rec = verify(_record(po_number=None), corroboration)
    v = rec["verification"]
    assert v["agreement"]["po_number"] == "agree"          # both null = both see absence
    assert v["agreement_score"] == "9/9"


def test_agreement_scoring_flags_witness_disagreement():
    corroboration = {
        "vendor_name": "EuroFuel Gmbh",  # witness differs by case
        "vendor_vat": "DE123456789",
        "invoice_number": "INV-2025-03701", "invoice_date": "2025-06-05",
        "po_number": "PO-55210", "currency": "EUR",
        "subtotal": Decimal("7186.28"), "vat_amount": Decimal("1365.39"),
        "total": Decimal("8551.67"),
    }
    rec = verify(_record(), corroboration)
    v = rec["verification"]
    assert v["agreement"]["vendor_name"] == "disagree"
    assert v["agreement_score"] == "8/9"
    assert any("vendor_name" in f for f in v["review_flags"])


def test_self_consistent_ai_misread_is_caught_by_the_second_read():
    """SparkIT failure mode: the AI misreads EVERY number the same way, so its own
    arithmetic is self-consistent. The independent witness is what catches it."""
    rec = verify(
        _record(subtotal=Decimal("6.30"), vat_amount=Decimal("0.70"), total=Decimal("7.00")),
        corroboration={  # the witness read the printed document correctly
            "vendor_name": "SparkIT BV", "vendor_vat": "NL123456789B01",
            "invoice_number": "INV-2025-03752", "invoice_date": "2025-06-24",
            "po_number": None, "currency": "EUR",
            "subtotal": Decimal("6302.51"), "vat_amount": Decimal("1197.48"),
            "total": Decimal("7499.99"),
        },
    )
    v = rec["verification"]
    assert v["authority_inputs_agree"] is False
    assert any("authority_inputs_disagree" in f for f in v["review_flags"])
    assert rec["total_trusted"] is None  # nothing is published as trusted


def test_wrapped_total_is_joined_by_verification():
    """StationOps shape: TOTAL / '€10,010.4' / '6' — the tokens join to the authority."""
    rec = verify(_record(
        subtotal=Decimal("8412.99"), vat_amount=Decimal("1597.47"), total=Decimal("10010.4"),
        total_raw={"line_text": "TOTAL", "tokens": ["€10,010.4", "6"]},
    ), corroboration={
        "vendor_name": "StationOps GmbH", "vendor_vat": "DE987654321",
        "invoice_number": "INV-2025-03760", "invoice_date": "2025-07-01",
        "po_number": "PO-55310", "currency": "EUR",
        "subtotal": Decimal("8412.99"), "vat_amount": Decimal("1597.47"),
        "total": Decimal("10010.4"),
    })
    v = rec["verification"]
    assert v["wrap_detected"] is True
    assert rec["total_trusted"] == Decimal("10010.46")
