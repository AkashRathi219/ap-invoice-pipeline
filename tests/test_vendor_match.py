"""Vendor master link tests — L1 exact -> L2 fuzzy -> L3 unknown (offline, no LLM)."""
from decimal import Decimal

from src.vendor_match import VendorRow, load_vendor_master, match_vendor


def _row(vendor_id="V015", vendor_name="QuickFix OÜ", status="ACTIVE", country="Estonia",
        approved_currency="EUR", spend_threshold_eur=Decimal("10000"), requires_dual_approval=False):
    return VendorRow(
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        category="Maintenance",
        payment_terms_days=30,
        approved_currency=approved_currency,
        spend_threshold_eur=spend_threshold_eur,
        status=status,
        country=country,
        contact_email="billing@example.com",
        requires_dual_approval=requires_dual_approval,
    )


def test_exact_vendor_match():
    master = [_row("V001", "EuroFuel GmbH", country="Germany")]
    m = match_vendor("EuroFuel GmbH", "Germany", master)
    assert m.status == "exact"
    assert m.vendor_id == "V001"
    assert m.fuzzy_score == 1.0
    assert m.country_match is True


def test_fuzzy_vendor_match_above_threshold_with_diacritic_variant():
    """QuickFix trap: diacritic/casing variant of the same vendor name -> fuzzy, ratio >= 0.85."""
    master = [_row("V015", "QuickFix OAo", status="ACTIVE", country="Estonia")]
    m = match_vendor("QuickFix OÜ", "Estonia", master)
    assert m.status == "fuzzy"
    assert m.vendor_id == "V015"
    assert m.fuzzy_score >= 0.85


def test_unknown_vendor_match():
    master = [_row("V009", "SparkIT BV", status="ACTIVE", country="Netherlands")]
    m = match_vendor("LogiTrans Express SRL", "Romania", master)
    assert m.status == "unknown"
    assert m.vendor_id is None


def test_inactive_vendor_resolves_exactly():
    master = [_row("V018", "OldGuard Security NV", status="INACTIVE", country="Belgium")]
    m = match_vendor("OldGuard Security NV", "Belgium", master)
    assert m.status == "exact"
    assert m.vendor_row.status == "INACTIVE"


def test_committed_master_loads_25_rows():
    rows = load_vendor_master()
    assert len(rows) == 25
    assert rows[0].vendor_id == "V001"
    assert rows[-1].vendor_id == "V025"
