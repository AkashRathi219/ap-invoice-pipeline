"""Review tests — the HITL menu, the append-order-wins journal, and the run.py review wiring.

Fully offline: the menu is driven with a scripted input_fn / no-op print_fn into a tmp_path
journal (never the committed output/), and run.py is exercised over --no-ai --limit 1 with an
explicit --out-dir. The two exception records reuse the exact shapes from test_feedback.py:
(a) a plain HOLD (SparkIT, R6) and (b) the flagged clone with a CRITICAL impostor verdict on
vendor category "Station Operations" (OldGuard, R1 + R5a).
"""
import hashlib
import sys
import types

import pytest

import run
from src import feedback, review, summarize
from src.decide import decide_all

HOLD = "HOLD"
CLEAR = "CLEAR"

CLONE_INVOICE = "INV-2025-03880"       # OldGuard — the flagged clone exception
PO_HOLD_INVOICE = "INV-2025-03752"     # SparkIT — the plain R6 hold


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


def _decided_records() -> list[dict]:
    """The two exception records (test_feedback shapes) with S10 decisions attached.

    summarize.exception_records orders them worst-first: the clone (HOLD + CRITICAL impostor
    risk) is asked before the plain PO hold (HOLD, HIGH), so scripted menu inputs target the
    clone when a single action is followed by QUIT.
    """
    records = [
        _record({"R6": {"status": HOLD, "reason_code": "R6_po_missing"}},
                invoice_number=PO_HOLD_INVOICE, vendor_name="SparkIT BV",
                vendor_match={"status": "exact", "vendor_id": "V009"},
                vendor_master={"category": "IT Services", "status": "ACTIVE"},
                fx={"amount_eur": "7499.99", "fx_known": True}),
        _record({"R1": {"status": HOLD, "reason_code": "R1_inactive_vendor"},
                 "R5a": {"status": "flag", "reason_code": "R5a_clone_package"}},
                invoice_number=CLONE_INVOICE, vendor_name="OldGuard Security NV",
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


def _inputs(values):
    """A scripted terminal: pops one value per prompt and logs every prompt it was asked.

    Asserts it is never over-consumed — the menu re-prompting forever surfaces here as an
    explicit failure instead of a hang. Scripted leftovers are fine (a trailing QUIT is a
    safety net that need not be reached). `scripted.log` keeps the prompt sequence for
    re-prompt assertions.
    """
    pending = list(values)
    log: list[str] = []

    def scripted(prompt: str = "") -> str:
        log.append(prompt)
        assert pending, f"inputs over-consumed: {len(log)} prompt(s) asked, scripted {values!r}"
        return pending.pop(0)

    scripted.log = log
    return scripted


def _silent(*_args, **_kwargs) -> None:
    """print_fn stand-in: console output is swallowed; the assertions read the journal."""


# --------------------------------------------------------------------------------- the journal

def test_load_events_absent_journal_is_empty(tmp_path):
    assert review.load_events(tmp_path) == []


def test_accept_journals_valid_schema(tmp_path):
    records = _decided_records()
    events = review.run_review(records, tmp_path, input_fn=_inputs(["A", "S", "Q"]),
                               print_fn=_silent)
    assert review.journal_path(tmp_path).exists()
    loaded = review.load_events(tmp_path)
    assert len(loaded) == 1 and len(events) == 1
    assert loaded[0]["action"] == "ACCEPT"
    assert set(loaded[0]) >= {"ts", "invoice_id", "action", "note", "override_disposition"}


def test_reject_sets_final_disposition_to_clear(tmp_path):
    records = _decided_records()
    before = [r["decision"]["disposition"] for r in records]
    review.run_review(records, tmp_path, input_fn=_inputs(["R", "Q"]), print_fn=_silent)
    assert [r["decision"]["disposition"] for r in records] == before   # records never mutated

    rows = review.final_dispositions(records, review.load_events(tmp_path))
    clone = next(r for r in rows if r["invoice_number"] == CLONE_INVOICE)
    assert clone["reviewer_action"] == "REJECT"
    assert clone["final_disposition"] == CLEAR
    assert clone["machine_disposition"] != CLEAR


def test_override_validates_target_and_reprompts(tmp_path):
    records = _decided_records()

    # "banana" is not a disposition: the sub-prompt re-asks, the menu never advances
    boxes = _inputs(["O", "banana", "C", "Q"])
    events = review.run_review(records, tmp_path, input_fn=boxes, print_fn=_silent)
    assert len(events) == 1
    assert events[0]["invoice_id"] == CLONE_INVOICE
    assert events[0]["action"] == "OVERRIDE" and events[0]["override_disposition"] == CLEAR
    override_prompts = [p for p in boxes.log if "Override to" in p]
    action_prompts = [p for p in boxes.log if "action for" in p]
    assert len(override_prompts) == 2            # re-prompted within the same sub-prompt
    assert len(action_prompts) == 2              # each exception asked exactly once

    # lowercase works too: o / h / q -> OVERRIDE -> HOLD (fresh journal, no prior)
    lower = _inputs(["o", "h", "q"])
    events = review.run_review(records, tmp_path / "lower", input_fn=lower, print_fn=_silent)
    assert len(events) == 1
    assert events[0]["action"] == "OVERRIDE" and events[0]["override_disposition"] == HOLD


def test_unrecognised_action_reprompts_then_accepts(tmp_path):
    records = _decided_records()

    # "MAYBE" is not a review action: the menu re-asks the same item, then ACCEPT lands once
    boxes = _inputs(["MAYBE", "A", "Q"])
    events = review.run_review(records, tmp_path, input_fn=boxes, print_fn=_silent)
    assert len(events) == 1
    assert events[0]["invoice_id"] == CLONE_INVOICE
    assert events[0]["action"] == "ACCEPT"
    clone_prompts = [p for p in boxes.log if f"action for {CLONE_INVOICE}" in p]
    hold_prompts = [p for p in boxes.log if f"action for {PO_HOLD_INVOICE}" in p]
    assert len(clone_prompts) == 2              # the unrecognised token re-prompted the same item
    assert len(hold_prompts) == 1               # the menu only advanced after a valid action
    assert len(review.load_events(tmp_path)) == 1


def test_newest_event_wins_across_runs(tmp_path):
    records = _decided_records()

    # ACCEPT, then REJECT for the same invoice (the menu's first exception) -> REJECT wins
    review.run_review(records, tmp_path, input_fn=_inputs(["A", "Q"]), print_fn=_silent)
    review.run_review(records, tmp_path, input_fn=_inputs(["R", "Q"]), print_fn=_silent,
                      prior=review.load_events(tmp_path))
    assert [e["invoice_id"] for e in review.load_events(tmp_path)] == [
        CLONE_INVOICE, CLONE_INVOICE]
    rows = review.final_dispositions(records, review.load_events(tmp_path))
    clone = next(r for r in rows if r["invoice_number"] == CLONE_INVOICE)
    assert clone["reviewer_action"] == "REJECT" and clone["final_disposition"] == CLEAR

    # reverse order: REJECT first, then ACCEPT -> the machine disposition stands again
    reverse = review.run_review(
        records, tmp_path / "reverse", input_fn=_inputs(["R", "Q"]), print_fn=_silent)
    reverse = review.run_review(records, tmp_path / "reverse", input_fn=_inputs(["A", "Q"]),
                                print_fn=_silent, prior=review.load_events(tmp_path / "reverse"))
    assert [e["invoice_id"] for e in reverse] == [CLONE_INVOICE, CLONE_INVOICE]
    rows = review.final_dispositions(records, reverse)
    clone = next(r for r in rows if r["invoice_number"] == CLONE_INVOICE)
    assert clone["reviewer_action"] == "ACCEPT"
    assert clone["final_disposition"] == clone["machine_disposition"] == HOLD


def test_reject_of_flagged_impostor_raises_loop_a_suggestion():
    records = _decided_records()
    rows = [feedback.run_telemetry_row(r) for r in records]
    suggestion = feedback.recommend_thresholds(
        rows, [{"invoice_id": CLONE_INVOICE, "action": "REJECT"}])
    assert suggestion["rejected_flags"] == 1
    category = suggestion["categories"]["Station Operations"]
    assert category["current"] == 0.85 and category["suggested"] == 0.90   # baseline +0.05


# ------------------------------------------------------------------------------ the menu itself

def test_quit_preserves_prior_actions(tmp_path):
    records = _decided_records()
    feedback.record_feedback(review.journal_path(tmp_path),
                             [{"invoice_id": PO_HOLD_INVOICE, "action": "ACCEPT"}])
    prior = review.load_events(tmp_path)
    assert len(prior) == 1

    events = review.run_review(records, tmp_path, input_fn=_inputs(["Q"]),
                               print_fn=_silent, prior=prior)
    assert events == prior                          # QUIT adds nothing, drops nothing
    assert len(review.load_events(tmp_path)) == 1

    # two exceptions: "A" then "Q" adds exactly one new event (1 prior + 1 new = 2 total)
    events = review.run_review(records, tmp_path, input_fn=_inputs(["A", "Q"]),
                               print_fn=_silent, prior=review.load_events(tmp_path))
    assert len(events) == 2
    assert len(review.load_events(tmp_path)) == 2


def test_render_review_report_has_three_parts():
    records = _decided_records()
    report = review.render_review_report(
        records, [{"invoice_id": CLONE_INVOICE, "action": "REJECT"}])
    assert "FIRST RUN" in report
    assert "FEEDBACK RECEIVED" in report
    assert "FINAL RESULT" in report


def test_write_review_summary_creates_file(tmp_path):
    records = _decided_records()
    events = [{"invoice_id": CLONE_INVOICE, "action": "REJECT"},
              {"invoice_id": PO_HOLD_INVOICE, "action": "OVERRIDE",
               "override_disposition": HOLD}]
    rows = [feedback.run_telemetry_row(r) for r in records]
    suggestion = feedback.recommend_thresholds(rows, events)

    path = review.write_review_summary(tmp_path, records, events, suggestion)
    text = path.read_text(encoding="utf-8")
    assert path.name == "review_summary.md"
    assert text.strip()                          # non-empty markdown
    assert "# Interactive Review Summary" in text
    for heading in ("FIRST RUN", "FEEDBACK RECEIVED", "FINAL RESULT"):
        assert f"## {heading}" in text
    assert "| Disposition | Count |" in text      # the disposition tables render as markdown
    assert "| Action | Count |" in text
    assert "| REJECT | 1 |" in text and "| OVERRIDE | 1 |" in text
    assert f"- {CLONE_INVOICE} : REJECT" in text  # the journal events are listed per invoice
    assert f"- {PO_HOLD_INVOICE} : OVERRIDE -> {HOLD}" in text
    assert "Loop A:" in text                      # the Loop A threshold hint is carried over
    assert "Station Operations" in text


def test_previous_review_line_shows_override_target(tmp_path):
    records = _decided_records()
    feedback.record_feedback(review.journal_path(tmp_path),
                             [{"invoice_id": CLONE_INVOICE, "action": "OVERRIDE",
                               "override_disposition": CLEAR},
                              {"invoice_id": PO_HOLD_INVOICE, "action": "ACCEPT"}])
    printed: list[str] = []
    review.run_review(records, tmp_path, input_fn=_inputs(["S", "Q"]),
                      print_fn=printed.append, prior=review.load_events(tmp_path))
    text = "\n".join(printed)
    assert f"Previous review: OVERRIDE -> {CLEAR}" in text   # the target is shown, not hidden
    assert "Previous review: ACCEPT" in text                 # plain actions keep the old shape
    assert len(review.load_events(tmp_path)) == 2            # SKIP/QUIT still record nothing


# ----------------------------------------------------------------------------------- run.py wiring

def test_run_main_without_review_never_reads_stdin(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stdin read without --review")))
    rc = run.main(["--no-ai", "--limit", "1", "--out-dir", str(tmp_path / "out"),
                   "--data-dir", str(data_dir)])
    assert rc == 0


def test_run_main_review_non_tty_prints_notice(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stdin read without --review")))
    rc = run.main(["--no-ai", "--limit", "1", "--out-dir", str(tmp_path / "out"),
                   "--data-dir", str(data_dir), "--review"])
    assert rc == 0
    assert "not an interactive terminal" in capsys.readouterr().out


# ------------------------------------------------ machine artefact immutability (R7, on disk)

def test_machine_artefacts_byte_identical_after_review_overlay(tmp_path):
    records = _decided_records()
    meta = {"generated_at": "2026-10-17T00:00:00+00:00", "model": "test-model",
            "prompt_version": "v-test", "temperature": 0.0, "backend": "test-backend",
            "ai_available": False}
    summarize.build_outputs(records, meta, tmp_path)
    machine = ("summary.md", "report.html", "batch_report.json",
               "audit_log.jsonl", "exceptions.csv", "invoices_processed.csv")
    before = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
              for name in machine}

    # the review overlay, applied exactly as run.py does: journal -> replay -> write_all -> summary
    telemetry_rows = [feedback.run_telemetry_row(r) for r in records]
    feedback.record_feedback(review.journal_path(tmp_path),
                             [{"invoice_id": CLONE_INVOICE, "action": "REJECT"}])
    events = review.load_events(tmp_path)
    feedback.write_all(records, tmp_path, tmp_path, telemetry_rows=telemetry_rows, feedback=events)
    review.write_review_summary(tmp_path, records, events,
                                feedback.recommend_thresholds(telemetry_rows, events))

    for name, digest in before.items():          # the machine artefacts are byte-identical
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest, name
    assert review.journal_path(tmp_path).is_file()
    assert (tmp_path / review.SUMMARY_NAME).is_file()
    assert [e["action"] for e in review.load_events(tmp_path)] == ["REJECT"]
