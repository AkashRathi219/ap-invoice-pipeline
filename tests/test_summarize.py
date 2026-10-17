"""Summarize tests — the S11 artefacts, prioritisation and the runtime oracle check (offline)."""
import csv
import json
from pathlib import Path

import pytest

from src import summarize
from src.decide import decide_all
from src.decide import HOLD  # noqa: F401  (used in test asserts)
from src.summarize import (
    build_outputs,
    decision_of,
    disposition_of,
    exception_records,
    load_oracle,
    measured_totals,
    render_report_html,
    verify_oracle,
    write_exceptions_csv,
    write_invoices_csv,
    write_summary_md,
)

from tests.test_decide import ORACLE, _oracle_record, _record, _result


# ------------------------------------------------------------------------------ golden fixture

def test_oracle_reproduces_stated_totals_from_the_ladder():
    records = [_oracle_record(invoice_number, spec) for invoice_number, spec in ORACLE["invoices"].items()]
    decide_all(records, llm_cfg=None)
    result = verify_oracle(records, ORACLE)
    assert result["ok"], result
    assert result["expected_totals"] == {"CLEAR": 11, "FLAG-REVIEW": 3, "HOLD": 3}
    assert measured_totals(records) == result["expected_totals"]


def test_verify_oracle_reports_mismatch_and_missing():
    good = _oracle_record("INV-2025-03701", ORACLE["invoices"]["INV-2025-03701"])
    bad = _oracle_record("INV-2025-03880", ORACLE["invoices"]["INV-2025-03880"])
    bad["rules"]["results"] = [_result("R1", "pass")]  # drifted from HOLD
    result = verify_oracle([good, bad], ORACLE)
    assert result["ok"] is False
    assert result["mismatches"] == [{"invoice_number": "INV-2025-03880",
                                     "expected": "HOLD", "measured": "CLEAR"}]
    assert result["missing_invoices"]  # the other 15 are unprocessed


def test_load_oracle_reads_the_committed_fixture():
    oracle = load_oracle()
    assert oracle["source"].startswith("DATA_MAP_AND_BUILD_PLAN")
    assert "INV-2025-03744" in oracle["invoices"]


# ------------------------------------------------------------------------------- build_outputs

@pytest.fixture()
def batch(tmp_path):
    """The 17 oracle records, decided, with the extra tells the artefacts render."""
    records = []
    for invoice_number, spec in ORACLE["invoices"].items():
        record = _oracle_record(invoice_number, spec)
        record["source_file"] = f"{invoice_number}.pdf"
        record["vendor_name"] = f"Vendor {invoice_number[-5:]}"
        record["currency"] = "EUR"
        record["total"] = record["total_trusted"] = "1000.00"
        record["fx"] = {"amount_eur": "1000.00", "rate_to_eur": 1.0, "as_of": "2025-11-30"}
        record["vendor_match"] = {"status": "exact", "vendor_id": "V001"}
        record["vendor_master"] = {"status": "ACTIVE", "spend_threshold_eur": "20000"}
        record["vendor_country"] = "Germany"
        record["verification"] = {"agreement_score": "9/9", "line_math_ok": True,
                                  "total_status": "clean"}
        record["duplicates"] = {"vs_historic": {"status": "skip"}, "within_batch": [],
                                "impostor_verdicts": []}
        records.append(record)
    decide_all(records, llm_cfg=None)
    return records


def test_build_outputs_writes_every_artefact(batch, tmp_path):
    meta = {"generated_at": "2026-10-17T08:00:00Z", "model": "llama3.2:3b",
            "prompt_version": "ap-extract-v1", "temperature": 0.0,
            "backend": "char4gram-cosine", "ai_available": True,
            "control": {"found": 17, "expected": 17, "processed": 17}}
    result = build_outputs(batch, meta, tmp_path)
    assert result["oracle"]["ok"]
    for name in ("invoices_processed.csv", "exceptions.csv", "summary.md", "batch_report.json",
                 "audit_log.jsonl", "report.html"):
        assert Path(result["files"][name]).is_file()
    audit_lines = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(audit_lines[0])["event"] == "RUN_START"
    assert len(audit_lines) == 18  # 1 run header + 17 invoice events
    report = json.loads((tmp_path / "batch_report.json").read_text(encoding="utf-8"))
    assert report["oracle"]["ok"] and report["dispositions"] == ORACLE["totals"]
    assert all(env["phase"] == "DECIDE" for env in report["invoices"])
    html_report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "ORACLE MATCH" in html_report and "{{" not in html_report  # every token rendered
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Oracle §5.3 check:** MATCH" in summary
    assert "17 invoices were read" in summary


def test_exceptions_csv_prioritises_hold_then_flag(batch, tmp_path):
    path = write_exceptions_csv(batch, tmp_path / "exceptions.csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [r["disposition"] for r in rows] == [
        "HOLD", "HOLD", "HOLD", "FLAG-REVIEW", "FLAG-REVIEW", "FLAG-REVIEW"]
    assert rows[0]["priority"] == "1"
    # evidence carries the scored proof, not prose
    sparkit = [r for r in rows if r["invoice_number"] == "INV-2025-03752"][0]
    assert "R6_po_missing" in sparkit["reason_codes"]
    assert "threshold" in sparkit["evidence"]
    # the dual-read failure (CleanStation) is flagged, not silently cleared
    cleanstation = [r for r in rows if r["invoice_number"] == "INV-2025-03744"][0]
    assert "R3_amount_unavailable" in cleanstation["reason_codes"]


def test_invoices_csv_one_row_per_invoice(batch, tmp_path):
    path = write_invoices_csv(batch, tmp_path / "invoices.csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert len(rows) == 17
    assert all(r["disposition"] in ("CLEAR", "FLAG-REVIEW", "HOLD") for r in rows)
    cleanstation = [r for r in rows if r["invoice_number"] == "INV-2025-03744"][0]
    assert cleanstation["disposition"] == "FLAG-REVIEW"  # oracle: dual-read fails closed


def test_counterparty_and_evidence_for_clone_pairs(tmp_path):
    from tests.test_decide import _record

    record = _record({"R5a": {"status": "flag", "reason_code": "R5a_clone_package"}},
                     invoice_number="INV-2025-03760", vendor_name="StationOps GmbH",
                     duplicates={"within_batch": [{"a_invoice": "INV-2025-03760",
                                                   "b_invoice": "INV-2025-03955",
                                                   "a_vendor": "StationOps GmbH",
                                                   "b_vendor": "LogiTrans Express SRL",
                                                   "description_cosine": 1.0,
                                                   "quantity_match": 1.0,
                                                   "unit_price_ratio": 0.692,
                                                   "pattern": "CLONE_PACKAGE_BLOCKED_VENDOR",
                                                   "flagged": True,
                                                   "embedding_backend": "char4gram-cosine"}],
                                 "impostor_verdicts": [
                                     {"counterparty_invoice": "INV-2025-03955",
                                      "counterparty_vendor": "LogiTrans Express SRL",
                                      "verdict": {"risk_level": "CRITICAL", "source": "llm"}}]})
    decide_all([record], llm_cfg=None)
    path = write_exceptions_csv([record], tmp_path / "exceptions.csv")
    row = list(csv.DictReader(path.open(encoding="utf-8-sig")))[0]
    assert row["counterparty"] == "INV-2025-03955 (LogiTrans Express SRL)"
    assert "impostor risk CRITICAL" in row["evidence"]
    html_report = render_report_html([record], {"model": "m", "prompt_version": "p",
                                                "temperature": 0, "backend": "b",
                                                "generated_at": "t", "ai_available": False},
                                     {"ok": False, "expected_totals": {}})
    assert "CLONE_PACKAGE_BLOCKED_VENDOR" in html_report
    assert "StationOps GmbH" in html_report


def test_summary_md_lists_holds_before_flags_and_limitations(batch, tmp_path):
    path = write_summary_md(batch, {"generated_at": "t", "model": "m", "prompt_version": "p",
                                    "backend": "b"}, {"ok": True, "expected_totals": ORACLE["totals"]},
                            tmp_path / "summary.md")
    text = path.read_text(encoding="utf-8")
    assert text.index("### Held — payment blocked") < text.index("### Flagged for review")
    assert text.index("## Known limitations") > text.index("## How AI was used")
    assert "OldGuard" not in text  # generic vendors only; no real batch data


# -------------------------------------------------------------------------------- decision fallback

def test_decision_of_computes_when_s10_was_not_run():
    record = _record({"R6": {"status": HOLD, "reason_code": "R6_po_missing"}})
    assert disposition_of(record) == "HOLD"
    assert decision_of(record)["disposition"] == "HOLD"
    assert record.get("decision") is None  # decision_of never mutates the record


def test_counterparty_references_historic_duplicate(tmp_path):
    record = _record({
        "R5b": {"status": HOLD, "reason_code": "R5b_exact_duplicate"},
    })
    record["duplicates"] = {
        "vs_historic": {
            "exact_matches": [{"invoice_id": "INV-2024-99999", "status": "PAID"}],
            "near_matches": [],
        }
    }
    decide_all([record], llm_cfg=None)
    path = write_exceptions_csv([record], tmp_path / "exceptions.csv")
    row = list(csv.DictReader(path.open(encoding="utf-8-sig")))[0]
    assert row["counterparty"] == "Historic INV-2024-99999 (PAID)"


def test_counterparty_of_matches_private():
    plain = _record({"R1": "pass", "R2": "pass", "R3": "pass",
                     "R4": "pass", "R6": "pass"})
    impostor = _record({"R5a": {"status": "flag", "reason_code": "R5a_clone_package"}},
                       invoice_number="INV-2025-03760", vendor_name="StationOps GmbH",
                       duplicates={"within_batch": [], "impostor_verdicts": [
                           {"counterparty_invoice": "INV-2025-03955",
                            "counterparty_vendor": "LogiTrans Express SRL",
                            "verdict": {"risk_level": "CRITICAL", "source": "llm"}}]})
    clone = _record({"R5a": {"status": "flag", "reason_code": "R5a_clone_package"}},
                    invoice_number="INV-2025-03760", vendor_name="StationOps GmbH",
                    duplicates={"within_batch": [{"a_invoice": "INV-2025-03760",
                                                  "b_invoice": "INV-2025-03955",
                                                  "a_vendor": "StationOps GmbH",
                                                  "b_vendor": "LogiTrans Express SRL",
                                                  "description_cosine": 1.0,
                                                  "quantity_match": 1.0,
                                                  "unit_price_ratio": 0.692,
                                                  "pattern": "CLONE_PACKAGE_BLOCKED_VENDOR",
                                                  "flagged": True,
                                                  "embedding_backend": "char4gram-cosine"}],
                               "impostor_verdicts": []})
    historic = _record({
        "R5b": {"status": HOLD, "reason_code": "R5b_exact_duplicate"},
    })
    historic["duplicates"] = {
        "vs_historic": {
            "exact_matches": [{"invoice_id": "INV-2024-99999", "status": "PAID"}],
            "near_matches": [],
        }
    }
    records = [plain, impostor, clone, historic]
    decide_all(records, llm_cfg=None)
    assert [summarize.counterparty_of(r) for r in records] == [
        "",
        "INV-2025-03955 (LogiTrans Express SRL)",  # impostor verdict wins
        "INV-2025-03955 (LogiTrans Express SRL)",  # flagged within-batch pair (b side)
        "Historic INV-2024-99999 (PAID)",
    ]
    for record in records:  # the public delegate mirrors the private helper on every branch
        assert summarize.counterparty_of(record) == summarize._counterparty(record)


# ------------------------------------------------------------------ oracle scoping (R5, R6)

def _foreign_batch():
    """Three generic invoices whose numbers share nothing with the committed fixture."""
    records = []
    for i in range(1, 4):
        invoice_number = f"INV-CUSTOM-{i:04d}"
        record = _record({"R1": "pass", "R2": "pass", "R3": "pass",
                          "R4": "pass", "R6": "pass"},
                         invoice_number=invoice_number, vendor_name=f"Custom Vendor {i}")
        record["source_file"] = f"{invoice_number}.pdf"
        record["currency"] = "EUR"
        record["total"] = record["total_trusted"] = "500.00"
        record["fx"] = {"amount_eur": "500.00", "rate_to_eur": 1.0, "as_of": "2025-11-30"}
        record["vendor_match"] = {"status": "exact", "vendor_id": f"V90{i}"}
        record["vendor_master"] = {"status": "ACTIVE", "spend_threshold_eur": "20000"}
        record["vendor_country"] = "Germany"
        record["verification"] = {"agreement_score": "9/9", "line_math_ok": True,
                                  "total_status": "clean"}
        record["duplicates"] = {"vs_historic": {"status": "skip"}, "within_batch": [],
                                "impostor_verdicts": []}
        records.append(record)
    decide_all(records, llm_cfg=None)
    return records


def test_fully_foreign_batch_is_scoped_to_na():
    result = verify_oracle(_foreign_batch(), ORACLE)
    assert result["applicable"] is False
    assert result["status"] == "N/A"
    assert result["ok"] is True
    assert result["mismatches"] == []
    assert result["missing_invoices"] == []


def test_foreign_batch_artefacts_render_na(tmp_path):
    meta = {"generated_at": "2026-10-17T09:00:00Z", "model": "llama3.2:3b",
            "prompt_version": "ap-extract-v1", "temperature": 0.0,
            "backend": "char4gram-cosine", "ai_available": False,
            "control": {"found": 3, "expected": 3, "processed": 3}}
    result = build_outputs(_foreign_batch(), meta, tmp_path)
    assert result["oracle"]["ok"] and result["oracle"]["status"] == "N/A"
    report = json.loads((tmp_path / "batch_report.json").read_text(encoding="utf-8"))
    assert report["oracle"]["status"] == "N/A"
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Oracle §5.3 check:** N/A (custom dataset)" in summary
    assert "DIFF" not in summary and "INV-2025-" not in summary  # no mismatch/missing leak
    html_report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "ORACLE N/A — custom dataset" in html_report
    assert "DIFF" not in html_report and "{{" not in html_report
    assert "INV-2025-" not in html_report


def test_17_record_batch_still_reports_match(batch, tmp_path):
    result = verify_oracle(batch, ORACLE)
    assert result["ok"] is True
    assert result["status"] == "MATCH"
    assert result["applicable"] is True
    assert result["mismatches"] == [] and result["missing_invoices"] == []

    meta = {"generated_at": "2026-10-17T08:00:00Z", "model": "llama3.2:3b",
            "prompt_version": "ap-extract-v1", "temperature": 0.0,
            "backend": "char4gram-cosine", "ai_available": True,
            "control": {"found": 17, "expected": 17, "processed": 17}}
    build_outputs(batch, meta, tmp_path)
    audit_lines = (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 18  # 1 run header + 17 invoice events
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "17 invoices were read" in summary
    assert "Oracle §5.3 check:** MATCH" in summary
    assert "ORACLE MATCH" in (tmp_path / "report.html").read_text(encoding="utf-8")

