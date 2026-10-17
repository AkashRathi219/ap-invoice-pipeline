"""Rules tests — R1/R2/R3/R4/R6 reason codes + disposition ladder (offline)."""
from decimal import Decimal

from src.rules import CLEAR, HOLD, run_rules, check_currency, check_dual_approval, check_po, check_threshold, disposition
from src.vendor_match import VendorMatch, VendorRow, match_vendor


def _vendor_row(vendor_id="V006", status="ACTIVE", approved_currency="EUR", spend_threshold_eur=Decimal("20000"),
               requires_dual_approval=False):
    return VendorRow(
        vendor_id=vendor_id, vendor_name="Vendor GmbH", category="IT Services",
        payment_terms_days=30, approved_currency=approved_currency,
        spend_threshold_eur=spend_threshold_eur, status=status, country="United Kingdom",
        contact_email="billing@example.com", requires_dual_approval=requires_dual_approval,
    )


def _match(vendor_name="BritTech Solutions Ltd", status="ACTIVE", vendor_id="V006", row=None):
    return VendorMatch(
        vendor_name=vendor_name, status=status, vendor_id=vendor_id, vendor_name_matched=vendor_name,
        fuzzy_score=1.0 if status != "unknown" else None, country_match=True,
        vendor_row=row if row is not None else (_vendor_row(status=status) if status != "unknown" else None),
        reasons=(),
    )


def _record(**kw):
    base = {
        "vendor_name": "BritTech Solutions Ltd",
        "vendor_country": "United Kingdom",
        "currency": "GBP",
        "po_number": "PO-55331",
        "invoice_number": "INV-2025-03731",
    }
    base.update(kw)
    return base


def test_r1_unknown_vendor_is_hold():
    m = match_vendor("LogiTrans Express SRL", "Romania", [])
    results = run_rules(_record(), m, Decimal("6929.94"))
    assert results[0]["rule_id"] == "R1"
    assert results[0]["status"] == HOLD
    assert results[0]["reason_code"] == "R1_unknown_vendor"


def test_r1_inactive_vendor_is_hold():
    results = run_rules(_record(), _match(status="INACTIVE"), Decimal("11000.59"))
    assert results[0]["rule_id"] == "R1"
    assert results[0]["status"] == HOLD
    assert results[0]["reason_code"] == "R1_inactive_vendor"


def test_r2_currency_mismatch_is_flag():
    results = run_rules(_record(currency="USD"), _match(), Decimal("5857.16"))
    assert results[1]["rule_id"] == "R2"
    assert results[1]["status"] == "flag"
    assert results[1]["reason_code"] == "R2_currency_mismatch"


def test_r2_matching_currency_passes():
    row = _vendor_row(approved_currency="GBP")
    m = _match(row=row)
    results = run_rules(_record(currency="GBP"), m, Decimal("5857.16"))
    assert results[1]["status"] == "pass"
    assert results[1]["reason_code"] is None


def test_r3_above_threshold_after_fx_is_flag():
    """BritTech: GBP 4,998 -> EUR 5,857.16 (FX); above a 5,000 threshold -> flag R3."""
    above = check_threshold(Decimal("5857.16"), _vendor_row(spend_threshold_eur=Decimal("5000")))
    assert above["rule_id"] == "R3"
    assert above["status"] == "flag"
    assert above["reason_code"] == "R3_above_threshold"
    ok = check_threshold(Decimal("5857.16"), _vendor_row())
    assert ok["status"] == "pass"
    assert ok["reason_code"] is None


def test_r3_missing_amount_is_hold():
    assert check_threshold(None, _vendor_row())["status"] == HOLD
    assert check_threshold(None, _vendor_row())["reason_code"] == "R3_amount_unavailable"


def test_r4_dual_approval_is_flag():
    m = _match(vendor_name="SecureGuard AG", vendor_id="V007", status="ACTIVE",
               row=_vendor_row(vendor_id="V007", requires_dual_approval=True))
    results = run_rules(_record(), m, Decimal("18499.50"))
    assert results[3]["rule_id"] == "R4"
    assert results[3]["status"] == "flag"
    assert results[3]["reason_code"] == "R4_dual_approval_required"


def test_r6_missing_po_is_hold():
    results = run_rules(_record(po_number=None), _match(), Decimal("5857.16"))
    assert results[4]["rule_id"] == "R6"
    assert results[4]["status"] == HOLD
    assert results[4]["reason_code"] == "R6_po_missing"


def test_clean_invoice_lands_in_clears():
    m = _match(vendor_name="EuroFuel GmbH", vendor_id="V001")
    results = run_rules(_record(
        vendor_name="EuroFuel GmbH", vendor_country="Germany", currency="EUR",
        po_number="PO-55210", invoice_number="INV-2025-03701"),
        m, Decimal("8551.67"))
    assert disposition(results) == CLEAR
    assert all(r["status"] == "pass" for r in results)
