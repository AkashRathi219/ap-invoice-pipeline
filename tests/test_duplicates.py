"""R5 duplicate layer tests — R5b metadata (historic) and R5a semantic (within batch), offline.

The decisions pinned here:
  * an exact historic invoice/PO hit is a duplicate;
  * a recurring delivery (amount close, date-gap months away) is *evidence*, not a duplicate;
  * identical packages across two payment-eligible vendors are evidence, not a flag;
  * a clone package fires when one counterparty is not payment-eligible (unknown / INACTIVE).
"""
from decimal import Decimal

from src.duplicates import (
    Embedder,
    HistoricInvoice,
    align_line_items,
    check_historic,
    cosine,
    deterministic_verdict,
    find_within_batch_pairs,
    load_historic,
    pair_rule,
    quantity_match,
    service_line_items,
)


def _record(invoice, vendor, vendor_id, invoice_date, items, standing="ACTIVE",
            total="10000.00", po="PO-1", currency="EUR", note=None):
    return {
        "invoice_number": invoice,
        "vendor_name": vendor,
        "vendor_country": "Germany",
        "currency": currency,
        "invoice_date": invoice_date,
        "po_number": po,
        "total_trusted": total,
        "free_text_note": note,
        "line_items": [{"description": d, "qty": q, "unit_price": u, "amount": None}
                       for d, q, u in items],
        "vendor_match": {"status": "exact" if vendor_id else "unknown", "vendor_id": vendor_id},
        "vendor_master": {"status": standing, "contact_email": "ap@example.com",
                          "payment_terms_days": 30},
        "fx": {"amount_eur": total},
    }


def _historic(**kw) -> HistoricInvoice:
    base = dict(invoice_id="INV-2024-00001", vendor_id="V010", vendor_name="StationOps GmbH",
                invoice_date="2024-06-01", amount_eur=Decimal("10000.00"), currency="EUR",
                status="PAID", po_number="PO-999", payment_reference="PAY-1")
    base.update(kw)
    return HistoricInvoice(**base)


SERVICES = [
    ("Station supplies - restocking June 2025", 1, "3823.53"),
    ("Operational support staff - 80 hours", 80, "38.24"),
    ("Equipment calibration service", 2, "764.71"),
]


# ------------------------------------------------------------------------------- R5b: historic

def test_r5b_exact_invoice_number_is_a_duplicate():
    record = _record("INV-2024-00001", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    result = check_historic(record, [_historic()])
    assert result["status"] == "flag"
    assert result["reason_code"] == "R5b_exact_duplicate"
    assert result["exact_matches"][0]["link"] == "P4==H1"


def test_r5b_proximity_inside_tolerance_and_window_flags():
    record = _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    result = check_historic(record, [_historic(invoice_date="2025-06-10", amount_eur=Decimal("10050.00"))])
    assert result["status"] == "flag"
    assert result["reason_code"] == "R5b_near_duplicate"


def test_r5b_recurring_delivery_stays_unflagged_but_keeps_evidence():
    """Amount within 1% but a 7-month gap — business-normal, so evidence only."""
    record = _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    result = check_historic(record, [_historic(invoice_date="2024-11-01", amount_eur=Decimal("10050.00"))])
    assert result["status"] == "pass"
    assert result["reason_code"] is None
    evidence = result["near_matches"][0]
    assert evidence["within_tolerance"] is True
    assert evidence["within_window"] is False
    assert evidence["status"] == "PAID" and evidence["severity"] == 3


def test_r5b_far_amount_stays_unflagged_even_inside_the_window():
    record = _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    result = check_historic(record, [_historic(invoice_date="2025-06-20", amount_eur=Decimal("13000.00"))])
    assert result["status"] == "pass"
    assert result["near_matches"][0]["within_tolerance"] is False


def test_r5b_unknown_vendor_has_no_history_scope():
    record = _record("INV-2025-03955", "LogiTrans Express SRL", None, "2025-07-08", SERVICES)
    result = check_historic(record, [_historic()])
    assert result["status"] == "pass"
    assert result["vendor_history_count"] == 0


def test_committed_historic_file_loads(data_dir):
    rows = load_historic(data_dir / "historic_invoices.csv")
    assert len(rows) == 120
    assert all(r.vendor_id.startswith("V") for r in rows)


# --------------------------------------------------------------------------- R5a: within batch

def test_quantity_match_identical_and_scaled():
    from src.duplicates import align_line_items

    a = [{"description": SERVICES[0][0], "qty": 1}, {"description": SERVICES[1][0], "qty": 80}]
    b = [{"description": SERVICES[0][0], "qty": 1}, {"description": SERVICES[1][0], "qty": 40}]
    assert quantity_match(align_line_items(a, b)) == 0.75


def test_description_cosine_separates_unrelated_text():
    same = cosine(Embedder().encode(SERVICES[0][0]), Embedder().encode(SERVICES[0][0]))
    other = cosine(Embedder().encode(SERVICES[0][0]), Embedder().encode("Diesel supply Route DE-AT"))
    assert same == 1.0
    assert other < 0.5


def test_clone_package_flags_when_counterparty_is_blocked():
    """StationOps (approved) vs LogiTrans (no master record) — the planted impostor pair."""
    a = _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    b = _record("INV-2025-03955", "LogiTrans Express SRL", None, "2025-07-08",
                [(d, q, f"{float(u) * 0.6923:.2f}") for d, q, u in SERVICES],
                total="6929.94")
    pairs = find_within_batch_pairs([a, b], Embedder())
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.flagged is True
    assert pair.pattern == "CLONE_PACKAGE_BLOCKED_VENDOR"
    assert pair.quantity_match == 1.0
    assert pair.unit_price_ratio is not None and pair.unit_price_ratio < 1.0
    assert pair.a_standing != pair.b_standing
    verdict = deterministic_verdict(pair)
    assert verdict["risk_level"] == "CRITICAL"
    assert verdict["is_impostor_risk"] is True
    assert verdict["source"] == "deterministic"


def test_identical_package_across_active_vendors_is_not_flagged():
    """FleetMech / AutoServ / IberMaint quote the same standard package — evidence, not a flag."""
    a = _record("INV-2025-03712", "FleetMech GmbH", "V004", "2025-06-10", SERVICES)
    b = _record("INV-2025-03791", "IberMaint S.L.", "V014", "2025-07-07", SERVICES)
    pair = find_within_batch_pairs([a, b], Embedder())[0]
    assert pair.quantity_match == 1.0
    assert pair.flagged is False
    assert pair.pattern == "SHARED_PACKAGE_APPROVED_VENDORS"


def test_recurring_service_with_different_quantities_is_not_flagged():
    """Fuel trio: same route descriptions, different volumes -> recurring deliveries."""
    a = _record("INV-2025-03701", "EuroFuel GmbH", "V001", "2025-06-05",
                [("Diesel supply - Route DE-AT (bulk delivery)", 2291, "1.54"),
                 ("AdBlue additive supply", 200, "0.65")], total="8551.67")
    b = _record("INV-2025-03847", "TotalRoute S.A.", "V003", "2025-07-02",
                [("Diesel supply - Route DE-AT (bulk delivery)", 1537, "1.54"),
                 ("AdBlue additive supply", 200, "0.65")], total="5788.11")
    pair = find_within_batch_pairs([a, b], Embedder())[0]
    assert pair.description_cosine >= 0.85
    assert pair.pattern == "SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY"
    assert pair.flagged is False


def test_pair_rule_shape_and_disposition_effect():
    from src.rules import FLAG_REVIEW, disposition

    flagged_pair = find_within_batch_pairs([
        _record("INV-2025-03880", "OldGuard Security NV", "V018", "2025-07-05",
                [("Security personnel - 120 hours", 120, "64.71")], standing="INACTIVE"),
        _record("INV-2025-03901", "SecureGuard AG", "V007", "2025-07-01",
                [("Security personnel - 120 hours", 120, "108.82")]),
    ], Embedder())[0]
    rule = pair_rule(flagged_pair)
    assert rule["rule_id"] == "R5a"
    assert rule["status"] == "flag"
    assert rule["reason_code"] == "R5a_clone_package"
    assert disposition([rule]) == FLAG_REVIEW


def test_service_line_items_drops_totals_rows():
    record = _record("INV-X", "AutoServ S.R.L.", "V005", "2025-06-12",
                     SERVICES + [("Subtotal", 1, "4537.82"), ("VAT (19%)", 1, "862.19"),
                                 ("TOTAL", 1, "5400.01")])
    kept = [i["description"] for i in service_line_items(record)]
    assert kept == [d for d, _, _ in SERVICES]


# ------------------------------------------------------------------- verdicts: who owns severity

STUB_LLM_CFG = {"model": "stub", "base_url": "http://127.0.0.1:11434", "temperature": 0.0}


def _impostor_pair():
    return find_within_batch_pairs([
        _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES),
        _record("INV-2025-03955", "LogiTrans Express SRL", None, "2025-07-08", SERVICES,
                total="6929.94"),
    ], Embedder())[0]


def test_llm_grade_cannot_downgrade_the_gate(monkeypatch):
    """The measured failure mode: the model graded a confirmed clone LOW. The gate keeps CRITICAL."""
    from src import duplicates as dup

    a = _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    b = _record("INV-2025-03955", "LogiTrans Express SRL", None, "2025-07-08", SERVICES)
    pair = find_within_batch_pairs([a, b], Embedder())[0]
    monkeypatch.setattr(dup, "chat_json", lambda *args, **kw: _fake_result("LOW"))

    verdict = dup.forensic_verdict(pair, a, b, llm_cfg=STUB_LLM_CFG)
    assert verdict["source"] == "llm"
    assert verdict["ai_risk_level"] == "LOW"
    assert verdict["gate_risk_level"] == "CRITICAL"
    assert verdict["risk_level"] == "CRITICAL"
    assert verdict["gate_explanation"]
    assert verdict["explanation"] == "explanation from the model"


def _fake_result(risk: str):
    from src.llm_provider import LLMResult

    return LLMResult(
        data={"is_impostor_risk": False, "risk_level": risk, "pattern": "CLONE",
              "explanation": "explanation from the model"},
        model="stub", prompt_version="ap-forensic-v1", latency_ms=3,
        prompt_tokens=None, completion_tokens=None,
    )


def test_llm_grade_can_escalate_above_the_gate(monkeypatch):
    from src import duplicates as dup
    from src.duplicates import PairEvidence

    pair = PairEvidence(
        a_invoice="A", b_invoice="B", a_vendor="Approved One", b_vendor="Approved Two",
        a_vendor_id="V001", b_vendor_id="V002", a_standing="ACTIVE", b_standing="ACTIVE",
        cross_vendor=True, description_cosine=1.0, quantity_match=1.0, line_count_equal=True,
        unit_price_ratio=0.7, pattern="CLONE_PACKAGE_BLOCKED_VENDOR", flagged=True,
        backend="stub", paired_services=[],
    )
    assert dup.deterministic_verdict(pair)["risk_level"] == "HIGH"
    monkeypatch.setattr(dup, "chat_json", lambda *args, **kw: _fake_result("CRITICAL"))
    verdict = dup.forensic_verdict(pair, {"invoice_number": "A"}, {"invoice_number": "B"},
                                   llm_cfg=STUB_LLM_CFG)
    assert verdict["risk_level"] == "CRITICAL"
    assert dup.deterministic_verdict(pair)["gate_risk_level"] == "HIGH"


def test_verdict_falls_back_when_the_model_is_unavailable(monkeypatch):
    from src import duplicates as dup
    from src.llm_provider import LLMUnavailable

    a = _record("INV-2025-03760", "StationOps GmbH", "V010", "2025-06-22", SERVICES)
    b = _record("INV-2025-03955", "LogiTrans Express SRL", None, "2025-07-08", SERVICES)
    pair = find_within_batch_pairs([a, b], Embedder())[0]

    def _unreachable(*args, **kwargs):
        raise LLMUnavailable("ollama down")

    monkeypatch.setattr(dup, "chat_json", _unreachable)
    verdict = dup.forensic_verdict(pair, a, b, llm_cfg=STUB_LLM_CFG)
    assert verdict["source"] == "deterministic"
    assert verdict["risk_level"] == "CRITICAL"
    assert verdict["ai_risk_level"] is None
