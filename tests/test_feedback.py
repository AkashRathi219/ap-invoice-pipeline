"""Feedback tests — A5 telemetry + the three HITL improvement loops (offline)."""
import json

import pytest

from src.decide import decide_all
from src.feedback import (
    FEEDBACK_ACTIONS,
    fine_tuning_pairs,
    few_shot_exemplars,
    read_feedback,
    recommend_thresholds,
    record_feedback,
    run_telemetry_row,
    write_all,
    write_few_shot_bank,
    write_fine_tuning,
    write_telemetry,
)
from src.llm_provider import LLMResult

from tests.test_decide import _record
from tests.test_summarize import batch as batch_fixture  # noqa: F401


@pytest.fixture()
def decided():
    records = [
        _record(invoice_number="INV-2025-03701", vendor_name="EuroFuel GmbH",
                sha256="aa", extracted_at="2026-10-17T07:38:15+00:00",
                vendor_match={"status": "exact", "vendor_id": "V001"},
                vendor_master={"category": "Fuel", "status": "ACTIVE"},
                verification={"agreement_score": "9/9", "line_math_ok": True},
                fx={"amount_eur": "8551.67", "fx_known": True},
                extraction_layer="ai_primary", model="llama3.2:3b", prompt_version="ap-extract-v1"),
        _record({"R6": {"status": HOLD, "reason_code": "R6_po_missing"}},
                invoice_number="INV-2025-03752", vendor_name="SparkIT BV",
                vendor_match={"status": "exact", "vendor_id": "V009"},
                vendor_master={"category": "IT Services", "status": "ACTIVE"},
                fx={"amount_eur": "7499.99", "fx_known": True}),
        _record({"R1": {"status": HOLD, "reason_code": "R1_inactive_vendor"},
                 "R5a": {"status": "flag", "reason_code": "R5a_clone_package"}},
                invoice_number="INV-2025-03880", vendor_name="OldGuard Security NV",
                vendor_match={"status": "exact", "vendor_id": "V018"},
                vendor_master={"category": "Station Operations", "status": "INACTIVE"},
                fx={"amount_eur": "11000.59", "fx_known": True},
                duplicates={"within_batch": [{"a_vendor": "OldGuard Security NV",
                                              "b_vendor": "SecureGuard AG",
                                              "description_cosine": 1.0,
                                              "quantity_match": 1.0,
                                              "unit_price_ratio": 1.682,
                                              "flagged": True,
                                              "pattern": "CLONE_PACKAGE_BLOCKED_VENDOR"}],
                            "impostor_verdicts": [
                                {"counterparty_invoice": "INV-2025-03901",
                                 "counterparty_vendor": "SecureGuard AG",
                                 "verdict": {"source": "llm", "risk_level": "CRITICAL",
                                             "is_impostor_risk": True,
                                             "pattern": "CLONE_PACKAGE_BLOCKED_VENDOR",
                                             "gate_explanation": "identical services at 168%"}}]}),
    ]
    decide_all(records, llm_cfg=None)
    return records


HOLD = "HOLD"


# ----------------------------------------------------------------------------------- telemetry

def test_telemetry_row_carries_the_hitl_record(decided):
    row = run_telemetry_row(decided[0])
    assert row["telemetry_version"] == "ap-telemetry-v1"
    assert row["ts"] == "2026-10-17T07:38:15+00:00"  # extraction time, not write time
    assert row["disposition"] == "CLEAR"
    assert row["vendor_category"] == "Fuel"
    assert row["reviewer_feedback"] is None  # HITL slot — filled after a reviewer acts
    held = run_telemetry_row(decided[2])
    assert held["disposition"] == HOLD and held["impostor_risk"] == "CRITICAL"
    assert held["reason_codes"] == ["R1_inactive_vendor", "R5a_clone_package"]


def test_write_telemetry_is_one_json_per_line(decided, tmp_path):
    rows = [run_telemetry_row(r) for r in decided]
    path = write_telemetry(rows, tmp_path / "ai_telemetry.jsonl")
    loaded = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert [r["invoice_id"] for r in loaded] == [r["invoice_number"] for r in decided]


# ----------------------------------------------------------------------- reviewer actions (HITL)

def test_record_feedback_appends_and_validates_actions(tmp_path):
    path = record_feedback(tmp_path / "reviewer_feedback.jsonl", [
        {"invoice_id": "INV-2025-03880", "action": "ACCEPT", "note": "clone confirmed"}])
    record_feedback(path, [{"invoice_id": "INV-2025-03752", "action": "OVERRIDE",
                            "override_disposition": "CLEAR"}])
    events = read_feedback(path)
    assert [e["action"] for e in events] == ["ACCEPT", "OVERRIDE"]
    assert events[1]["override_disposition"] == "CLEAR"
    with pytest.raises(ValueError):
        record_feedback(tmp_path / "bad.jsonl", [{"action": "MAYBE"}])


def test_feedback_actions_vocabulary():
    assert FEEDBACK_ACTIONS == ("ACCEPT", "REJECT", "OVERRIDE")


# ---------------------------------------------------------------------------- Loop A: thresholds

def test_threshold_baseline_neutral_without_feedback(decided, tmp_path):
    rows = [run_telemetry_row(r) for r in decided]
    suggestions = recommend_thresholds(rows)
    assert suggestions["baseline_cosine_threshold"] == 0.85
    assert suggestions["rejected_flags"] == 0
    assert suggestions["categories"] == {}
    assert "never rewrites" in suggestions["note"]


def test_threshold_suggestion_rises_on_rejected_flag(decided, tmp_path):
    rows = [run_telemetry_row(r) for r in decided]
    suggestions = recommend_thresholds(rows, [{"invoice_id": "INV-2025-03880", "action": "REJECT"}])
    category = suggestions["categories"]["Station Operations"]
    assert category["current"] == 0.85 and category["suggested"] == 0.90
    assert suggestions["rejected_flags"] == 1


def test_threshold_suggestion_ignores_reject_of_non_flagged_invoice(decided):
    rows = [run_telemetry_row(r) for r in decided]
    suggestions = recommend_thresholds(rows, [{"invoice_id": "INV-2025-03701", "action": "REJECT"}])
    assert suggestions["rejected_flags"] == 0


# ---------------------------------------------------------------------------- Loop B: few-shot

def test_few_shot_exemplars_from_clone_hold_and_flag(decided):
    exemplars = few_shot_exemplars(decided)
    kinds = [e["kind"] for e in exemplars]
    assert "IMPOSTOR_CLONE" in kinds and "HOLD" in kinds
    clone = [e for e in exemplars if e["kind"] == "IMPOSTOR_CLONE"][0]
    assert "OldGuard Security NV" in clone["input"] and "1.682" in clone["input"]
    assert "CRITICAL" in clone["output"]
    assert "R6_po_missing" in [e["input"] for e in exemplars if e["kind"] == "HOLD"][0]


def test_few_shot_bank_written_with_version_and_seed_note(decided, tmp_path):
    path = write_few_shot_bank(decided, tmp_path / "few_shot_bank.json")
    bank = json.loads(path.read_text(encoding="utf-8"))
    assert bank["version"] == "ap-few-shot-v1"
    # SparkIT contributes the HOLD exemplar; the clone vendor contributes only the
    # IMPOSTOR_CLONE exemplar (its `continue` skips generic holds).
    assert len(bank["exemplars"]) == 2
    assert [e["kind"] for e in bank["exemplars"]] == ["HOLD", "IMPOSTOR_CLONE"]


# ---------------------------------------------------------------------------- Loop C: SFT export

def test_fine_tuning_pairs_only_review_required_and_clone_assessments(decided):
    pairs = fine_tuning_pairs(decided)
    assert [p["task"] for p in pairs] == [
        "AP_DISPOSITION_REVIEW",       # SparkIT hold
        "AP_DISPOSITION_REVIEW",       # OldGuard hold
        "IMPOSTOR_ASSESSMENT",         # OldGuard clone verdict
    ]
    hold_pair = pairs[0]
    assert hold_pair["format"] == "alpaca"
    assert json.loads(hold_pair["output"])["disposition"] == HOLD
    clone = pairs[2]
    assert json.loads(clone["output"])["is_impostor_risk"] is True
    assert json.loads(clone["output"])["risk_level"] == "CRITICAL"


def test_fine_tuning_dataset_header_counts_pairs(decided, tmp_path):
    path = write_fine_tuning(decided, tmp_path / "fine_tuning_dataset.jsonl")
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines[0]["event"] == "SFT_DATASET" and lines[0]["pair_count"] == len(lines) - 1 == 3


# -------------------------------------------------------------------------------- write_all

def test_write_all_produces_every_loop_artefact(decided, tmp_path):
    paths = write_all(decided, tmp_path, tmp_path / "config")
    for name in ("telemetry", "fine_tuning", "few_shot_bank", "threshold_suggestions"):
        assert paths[name]
    assert json.loads((tmp_path / "few_shot_bank.json").read_text(encoding="utf-8"))["exemplars"]
    suggestions = json.loads((tmp_path / "threshold_suggestions.json").read_text(encoding="utf-8"))
    assert suggestions["version"] == "ap-threshold-tuning-v1"


def test_telemetry_rationale_source_prefers_llm_when_used(decided, monkeypatch):
    from src import decide as decide_mod

    monkeypatch.setattr(
        decide_mod, "chat_json",
        lambda *a, **k: LLMResult(data={"summary": "PO missing; obtain it before release."},
                                  model="llama3.2:3b", prompt_version="ap-decide-v1",
                                  prompt_tokens=1, completion_tokens=1, latency_ms=1))
    decide_all([decided[1]], llm_cfg={"model": "llama3.2:3b", "base_url": "http://x", "temperature": 0})
    row = run_telemetry_row(decided[1])
    assert row["rationale_source"] == "llm"
