"""Decide tests — the S10 decision ladder, variant tells and the §5.3 golden-file oracle (offline).

The golden-file test drives `decide_record` over the committed oracle fixture
(`config/oracle_dispositions.json`): for each of the 17 invoices the fixture states the rule
outcomes that must produce the expected disposition. If the ladder drifts, this fails — the
oracle is itself the test fixture (SKL-ENG-09). No invoice data and no LLM are needed.
"""
import json
from pathlib import Path

import pytest

from src.decide import (
    CLEAR,
    FLAG_REVIEW,
    HOLD,
    QUEUE_BY_DISPOSITION,
    decide_all,
    decide_record,
    decision_reasons,
    evidence_summary,
    next_action_for,
    template_rationale,
    variant_tells,
)
from src.llm_provider import LLMResult
from src.summarize import DEFAULT_ORACLE_PATH

ORACLE = json.loads(DEFAULT_ORACLE_PATH.read_text(encoding="utf-8"))


def _result(rule_id: str, status: str, reason_code: str | None = None,
            detail: str = "test detail") -> dict:
    return {"rule_id": rule_id, "status": status, "reason_code": reason_code, "detail": detail}


def _record(rules: dict | None = None, **tells) -> dict:
    """Minimal record shaped like the real ones: rule results + the tells decide reads."""
    results = []
    for rule_id, spec in (rules or {}).items():
        if isinstance(spec, dict):
            results.append(_result(rule_id, spec["status"], spec.get("reason_code"),
                                   spec.get("detail", "test detail")))
        else:  # plain "pass"
            results.append(_result(rule_id, spec))
    return {"invoice_number": tells.pop("invoice_number", "INV-2025-00000"),
            "rules": {"results": results}, **tells}


# ------------------------------------------------------------------------- ladder + decision object

def test_ladder_clear_when_all_rules_pass():
    decision = decide_record(_record({"R1": "pass", "R2": "pass", "R3": "pass",
                                      "R4": "pass", "R6": "pass"}))
    assert decision["disposition"] == CLEAR
    assert decision["queue"] == QUEUE_BY_DISPOSITION[CLEAR] == "APPROVER_QUEUE"
    assert decision["review_required"] is False
    assert decision["reason_codes"] == []
    assert decision["blockers"] == []


def test_ladder_hold_beats_flag():
    results = [_result("R1", HOLD, "R1_inactive_vendor"), _result("R4", "flag", "R4_dual_approval_required")]
    decision = decide_record({"rules": {"results": results}})
    assert decision["disposition"] == HOLD
    assert decision["queue"] == "HELD"
    assert decision["outcome"] == "held_pending_completion"
    assert decision["review_required"] is True
    assert decision["reason_codes"] == ["R1_inactive_vendor", "R4_dual_approval_required"]


def test_flag_review_queue_and_next_action():
    decision = decide_record(_record({"R6": {"status": "flag", "reason_code": "R6_po_missing",
                                             "detail": "no PO reference on INV-2025-03752"}}))
    assert decision["disposition"] == FLAG_REVIEW
    assert decision["queue"] == "REVIEW_QUEUE"
    assert decision["next_action"] == next_action_for(["R6_po_missing"])
    assert "PO reference" in " ".join(decision["decision_reasons"])


def test_blockers_carry_rule_ids_and_details():
    decision = decide_record(_record({"R2": {"status": "flag", "reason_code": "R2_currency_mismatch"}}))
    assert decision["blockers"][0] == {"rule_id": "R2", "status": "flag",
                                       "reason_code": "R2_currency_mismatch", "detail": "test detail"}


# ---------------------------------------------------------------------------- risk level ownership

def test_risk_level_from_impostor_verdict_gate():
    record = _record({"R1": {"status": HOLD, "reason_code": "R1_inactive_vendor"}},
                     duplicates={"impostor_verdicts": [
                         {"rule_id": "R5a", "counterparty_invoice": "INV-2025-03901",
                          "counterparty_vendor": "SecureGuard AG",
                          "verdict": {"source": "deterministic", "risk_level": "CRITICAL",
                                      "pattern": "CLONE_PACKAGE_DIFFERENT_COUNTERPARTY",
                                      "gate_explanation": "identical services, 168% price"}}]})
    decision = decide_record(record)
    assert decision["risk_level"] == "CRITICAL"  # gate verdict, not the disposition
    assert decision["evidence"]["duplicates"]["impostor_risk"] == "CRITICAL"


def test_risk_level_defaults_from_disposition():
    assert decide_record(_record({"R6": {"status": HOLD, "reason_code": "R6_po_missing"}}))["risk_level"] == "HIGH"
    assert decide_record(_record({"R4": {"status": "flag", "reason_code": "R4_dual_approval_required"}}))["risk_level"] == "MEDIUM"
    assert decide_record(_record())["risk_level"] is None


# ------------------------------------------------------------------------------- variant tells F10

def test_variant_tells_v1_format_b_and_fallback_path():
    assert variant_tells(_record(format_tag="B"))[0]["variant_class"] == "FORMAT_VARIANT"
    assert variant_tells(_record(extraction_layer="regex_fallback"))[0]["variant_id"] == "V1"
    assert variant_tells(_record(format_tag="A", extraction_layer="ai_primary")) == []


def test_variant_tells_v2_v3_v4():
    v2 = variant_tells(_record(vendor_match={"status": "fuzzy", "fuzzy_score": 0.91}))
    assert any(t["variant_id"] == "V2" and t["tolerance"] == 0.85 for t in v2)
    v3 = variant_tells(_record(verification={"wrap_detected": True, "total_status": "wrap_resolved"}))
    assert any(t["variant_id"] == "V3" and t["severity"] == "critical" for t in v3)
    v4 = variant_tells(_record(currency="GBP", fx={"fx_known": True}))
    assert any(t["variant_id"] == "V4" for t in v4)


def test_variant_tells_v5_graduates_and_v6():
    flagged = [{"a_invoice": "A", "b_invoice": "B", "flagged": True}]
    scored = [{"a_invoice": "A", "b_invoice": "B", "flagged": False}]
    clone = variant_tells(_record(duplicates={"within_batch": scored,
                                              "impostor_verdicts": [{"verdict": {"risk_level": "CRITICAL"}}]}))
    assert any(t["variant_id"] == "V5" and t["severity"] == "critical" for t in clone)
    ev = variant_tells(_record(duplicates={"within_batch": flagged, "impostor_verdicts": []}))
    assert any(t["variant_id"] == "V5" and t["severity"] == "high" for t in ev)
    shared = variant_tells(_record(duplicates={"within_batch": scored, "impostor_verdicts": []}))
    assert any(t["variant_id"] == "V5" and t["severity"] == "low" for t in shared)
    v6 = variant_tells(_record({"R6": {"status": HOLD, "reason_code": "R6_po_missing"}}))
    assert any(t["variant_id"] == "V6" and t["variant_class"] == "DECISION_VARIANT" for t in v6)


def test_evidence_summary_rolls_up_the_scored_proof():
    evidence = evidence_summary(_record(
        {"R3": {"status": "flag", "reason_code": "R3_amount_unavailable"}},
        vendor_match={"status": "fuzzy", "fuzzy_score": 0.91},
        fx={"currency": "GBP", "amount_eur": "5857.16", "rate_to_eur": 1.1719, "as_of": "2025-11-30"},
        verification={"total_status": "mismatch", "agreement_score": "8/9"},
        vendor_master={"status": "ACTIVE", "spend_threshold_eur": "20000"},
        duplicates={"vs_historic": {"status": "skip", "reason_code": None}, "within_batch": []},
    ))
    assert evidence["vendor_match"]["fuzzy_score"] == 0.91
    assert evidence["fx"]["amount_eur"] == "5857.16"
    assert evidence["arithmetic"]["agreement_score"] == "8/9"
    assert evidence["duplicates"]["within_batch_published"] == 0


# ------------------------------------------------------------------------------- A4 rationale

def test_rationale_falls_back_to_template_without_llm():
    decision = decide_record(_record({"R1": {"status": HOLD, "reason_code": "R1_inactive_vendor"}}))
    assert decision["rationale"]["source"] == "template"
    assert "HOLD" in template_rationale(_record({"R1": {"status": HOLD, "reason_code": "R1_inactive_vendor"}}))
    assert "R1" in decision["rationale"]["text"]


def test_clear_invoices_get_no_rationale():
    decision = decide_record(_record())
    assert decision["rationale"] is None


def test_rationale_uses_llm_when_reachable(monkeypatch):
    from src import decide as decide_mod

    captured = {}

    def fake_chat_json(system, user, schema, **kwargs):
        captured["user"] = user
        return LLMResult(data={"summary": "Vendor is inactive; payment blocked until reactivation."},
                         model="llama3.2:3b", prompt_version="ap-decide-v1",
                         prompt_tokens=10, completion_tokens=10, latency_ms=5)

    monkeypatch.setattr(decide_mod, "chat_json", fake_chat_json)
    decision = decide_record(_record({"R1": {"status": HOLD, "reason_code": "R1_inactive_vendor"}}),
                             llm_cfg={"model": "llama3.2:3b", "base_url": "http://x", "temperature": 0})
    assert decision["rationale"]["source"] == "llm"
    assert "inactive" in decision["rationale"]["text"]
    assert "R1_inactive_vendor" in captured["user"]  # authoritative reasons passed verbatim


def test_decide_all_attaches_idempotent_decisions():
    records = [_record(invoice_number="A"), _record({"R6": {"status": HOLD, "reason_code": "R6_po_missing"}},
                                                    invoice_number="B")]
    decide_all(records, llm_cfg=None)
    decide_all(records, llm_cfg=None)
    assert records[0]["decision"]["disposition"] == CLEAR
    assert records[1]["decision"]["disposition"] == HOLD
    assert "R6" in " ".join(decision_reasons(records[1], HOLD))


def test_why_text_falls_back_when_detail_missing():
    from src.decide import why_text
    assert why_text({"rule_id": "R1", "reason_code": "R1_unknown_vendor", "detail": ""}) == "R1"


# --------------------------------------------------------------------- golden-file oracle §5.3

def _oracle_record(invoice_number: str, spec: dict) -> dict:
    results = []
    for rule_id, rule_spec in spec.get("rules", {}).items():
        results.append(_result(rule_id, rule_spec["status"], rule_spec.get("reason_code")))
    if not results:
        results = [_result("R1", "pass"), _result("R2", "pass"), _result("R3", "pass"),
                   _result("R4", "pass"), _result("R6", "pass")]
    return {"invoice_number": invoice_number, "rules": {"results": results}}


def test_oracle_fixture_is_committed_and_stated_totals():
    assert ORACLE["totals"] == {"CLEAR": 11, "FLAG-REVIEW": 3, "HOLD": 3}
    assert len(ORACLE["invoices"]) == 17


@pytest.mark.parametrize("invoice_number,spec", sorted(ORACLE["invoices"].items()),
                         ids=lambda v: str(v)[:24])
def test_oracle_invoice(invoice_number: str, spec: dict):
    decision = decide_record(_oracle_record(invoice_number, spec))
    assert decision["disposition"] == spec["expected"], invoice_number


def test_oracle_golden_file_run():
    """The §5.3 acceptance: full decision set == oracle (11 CLEAR / 3 FLAG-REVIEW / 3 HOLD)."""
    from src.summarize import measured_totals, verify_oracle

    records = [_oracle_record(invoice_number, spec) for invoice_number, spec in ORACLE["invoices"].items()]
    decide_all(records, llm_cfg=None)
    oracle_result = verify_oracle(records, ORACLE)
    assert oracle_result["ok"], oracle_result
    assert measured_totals(records) == ORACLE["totals"]
