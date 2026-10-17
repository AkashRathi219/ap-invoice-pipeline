"""S9 enrichment tests — note tells (P14) and message authoring (offline, template path).

Enrichment is additive by design: a vendor note is informational and never changes a disposition,
while every non-CLEAR invoice gets a message built strictly from the rule reason codes.
"""
from src.enrich import (
    ACTION_BY_REASON,
    amount_text,
    compose_message,
    enrich_record,
    note_tells,
)


def _record(disposition="HOLD", results=None, note=None, vendor_id="V009", standing="ACTIVE",
            currency="EUR", total="7499.99"):
    return {
        "invoice_number": "INV-2025-03752",
        "invoice_date": "2025-06-17",
        "vendor_name": "SparkIT BV",
        "currency": currency,
        "total_trusted": total,
        "free_text_note": note,
        "vendor_match": {"status": "exact", "vendor_id": vendor_id},
        "vendor_master": {"status": standing, "payment_terms_days": 30,
                          "contact_email": "billing@sparkit.example"},
        "rules": {"disposition": disposition, "results": results or []},
        "duplicates": {"vs_historic": {}, "within_batch": [], "impostor_verdicts": []},
    }


def _result(rule_id, status, reason_code, detail="because"):
    return {"rule_id": rule_id, "status": status, "reason_code": reason_code, "detail": detail}


def test_note_tells_classify_a_resubmission_without_blocking():
    tells = note_tells(_record(note="Note: Re-submission of delivery documentation for Route FR-DE corridor."))
    codes = {t["code"] for t in tells}
    assert "VENDOR_RESUBMISSION" in codes
    assert "DELIVERY_DOCUMENTATION" in codes
    assert all(t["impact"] == "informational" for t in tells)
    assert all(t["text"].startswith("Note:") for t in tells)


def test_no_note_means_no_tells():
    assert note_tells(_record()) == []


def test_compose_message_uses_the_reason_codes_and_the_vendor_contact():
    record = _record(results=[_result("R6", "HOLD", "R6_po_missing", "no PO reference")])
    message = compose_message(record)
    assert message["source"] == "template"
    assert message["channel"] == "vendor"
    assert message["to"] == "billing@sparkit.example"
    assert message["reason_codes"] == ["R6_po_missing"]
    assert message["next_action"] == ACTION_BY_REASON["R6_po_missing"]
    assert "R6_po_missing" in message["body"]
    assert "Next action:" in message["body"]


def test_unknown_vendor_message_stays_internal():
    record = _record(disposition="HOLD", vendor_id=None, standing=None,
                     results=[_result("R1", "HOLD", "R1_unknown_vendor", "not in master")])
    record["vendor_master"] = {}
    message = compose_message(record)
    assert message["channel"] == "internal"
    assert message["to"] == "ap-review-queue"
    assert "R1_unknown_vendor" in message["body"]


def test_message_mentions_duplicate_evidence_when_present():
    record = _record(disposition="FLAG-REVIEW",
                     results=[_result("R5a", "flag", "R5a_clone_package", "clone of INV-2025-03760")])
    record["rules"]["results"][0]["counterparty"] = "INV-2025-03760"
    message = compose_message(record)
    assert "clone-package match with invoice INV-2025-03760" in message["body"]


def test_author_message_falls_back_to_the_template_without_ai():
    record = _record(results=[_result("R6", "HOLD", "R6_po_missing")])
    out = enrich_record(record, llm_cfg=None)
    assert out["message"]["source"] == "template"
    assert out["message"]["model"] is None


def test_clear_invoice_gets_no_message_but_keeps_note_tells():
    record = _record(disposition="CLEAR", note="Note: Re-submission of delivery documentation.")
    out = enrich_record(record, llm_cfg=None)
    assert out["message"] is None
    assert out["note_tells"]


def test_amount_text_flags_an_unverified_total():
    record = _record(total=None)
    assert "unverified" in amount_text(record)


def test_llm_prose_keeps_the_deterministic_next_action(monkeypatch):
    """The model writes the prose; the actionable field stays owned by the rule layer."""
    from src import enrich as en
    from src.llm_provider import LLMResult

    record = _record(results=[_result("R6", "HOLD", "R6_po_missing", "no PO reference")])

    def _stub(*args, **kwargs):
        return LLMResult(
            data={"subject": "PO missing on INV-2025-03752",
                  "body": "This invoice cannot be paid until a PO reference is supplied.",
                  "next_action": "Ask the vendor for the PO number."},
            model="stub", prompt_version="ap-enrich-v1", latency_ms=5,
            prompt_tokens=None, completion_tokens=None,
        )

    monkeypatch.setattr(en, "chat_json", _stub)
    message = en.author_message(record, {"model": "stub", "base_url": "http://x"})
    assert message["source"] == "llm"
    assert message["model"] == "stub"
    assert message["next_action"] == ACTION_BY_REASON["R6_po_missing"]
    assert message["ai_next_action"] == "Ask the vendor for the PO number."
    assert message["reason_codes"] == ["R6_po_missing"]


def test_message_template_is_used_when_the_model_answers_garbage(monkeypatch):
    from src import enrich as en
    from src.llm_provider import LLMResult

    record = _record(results=[_result("R6", "HOLD", "R6_po_missing", "no PO reference")])

    def _empty(*args, **kwargs):
        return LLMResult(data={"subject": "", "body": ""}, model="stub",
                         prompt_version="ap-enrich-v1", latency_ms=5,
                         prompt_tokens=None, completion_tokens=None)

    monkeypatch.setattr(en, "chat_json", _empty)
    message = en.author_message(record, {"model": "stub", "base_url": "http://x"})
    assert message["source"] == "template"
    assert message["next_action"] == ACTION_BY_REASON["R6_po_missing"]
