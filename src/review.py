"""Interactive reviewer core — HITL ACCEPT / REJECT / OVERRIDE on the machine's exceptions.

The pipeline proposes; the reviewer disposes. `review.py` owns the menu loop and the review
artefacts:

* `reviewer_feedback.jsonl` — the append-only journal. `feedback.record_feedback` owns the
  format (ts, invoice_id, counterparty_invoice, action, note, override_disposition) and
  validates that `action` is one of ACCEPT / REJECT / OVERRIDE.
* `review_summary.md` — the before / after brief: machine dispositions (FIRST RUN), the
  reviewer actions actually received (FEEDBACK RECEIVED), and the dispositions after review
  (FINAL RESULT), with the Loop A threshold hint when a suggestion is supplied.

The menu itself is pure: every read goes through `input_fn`, every write through `print_fn`,
and journaling goes through `feedback.record_feedback` — so `run_review` is testable end to
end without a terminal (stdlib only; no file IO at import time).

Append order wins: `final_dispositions` resolves one winning event per invoice by keeping the
LAST event seen for that `invoice_id` (the journal `ts` has only second resolution, so it is
never compared).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from . import feedback, summarize

ACTIONS = ("ACCEPT", "REJECT", "OVERRIDE", "SKIP", "QUIT")
DISPOSITIONS = ("CLEAR", "FLAG-REVIEW", "HOLD")

JOURNAL_NAME = "reviewer_feedback.jsonl"
SUMMARY_NAME = "review_summary.md"

_DISPOSITION_CLASSES = DISPOSITIONS
# journaling classes are wired through ACTIONS, so the menu's vocabulary stays one source of truth
_FEEDBACK_CLASSES = tuple(a for a in ACTIONS if a in ("ACCEPT", "REJECT", "OVERRIDE"))

_ACTION_BY_INPUT = {
    "ACCEPT": "ACCEPT", "A": "ACCEPT",
    "REJECT": "REJECT", "R": "REJECT",
    "OVERRIDE": "OVERRIDE", "O": "OVERRIDE",
    "SKIP": "SKIP", "S": "SKIP",
    "QUIT": "QUIT", "Q": "QUIT",
}
_DISPOSITION_BY_INPUT = {
    "C": "CLEAR", "CLEAR": "CLEAR",
    "F": "FLAG-REVIEW", "FLAG": "FLAG-REVIEW", "FLAG-REVIEW": "FLAG-REVIEW",
    "H": "HOLD", "HOLD": "HOLD",
}


def journal_path(out_dir: Path | str) -> Path:
    """The reviewer journal for this run: `<out_dir>/reviewer_feedback.jsonl`.

    Pure path computation — the caller decides whether the file exists.
    """
    return Path(out_dir) / JOURNAL_NAME


def load_events(out_dir: Path | str) -> list[dict]:
    """Reads recorded reviewer actions for this run's out_dir from the append-only journal.

    Where called:
        Called by run.py in Pass 2 (and in tests) to replay prior review sessions.

    Where it sends information:
        Returns the parsed event list; [] when the journal does not exist yet
        (delegated to feedback.read_feedback).
    """
    return list(feedback.read_feedback(journal_path(out_dir)))


def final_dispositions(records: Iterable[dict], events: Iterable[dict] = ()) -> list[dict]:
    """Maps machine dispositions + reviewer events into one final row per record (input order).

    Where called:
        Called by render_review_report / write_review_summary, and in tests of the
        append-order-wins rule.

    Where it sends information:
        Returns a NEW list of dicts (records are never mutated):
        {invoice_number, source_file, vendor_name, machine_disposition,
         reviewer_action, final_disposition}.
    """
    winning: dict[str, dict] = {}
    for event in events:                      # list order; the LAST event per id wins
        winning[str(event.get("invoice_id"))] = event

    rows = []
    for record in records:
        machine = summarize.disposition_of(record)
        event = winning.get(str(record.get("invoice_number")))
        action = str(event.get("action") or "").upper() or None if event else None
        if action == "REJECT":
            final = "CLEAR"                   # the reviewer says the machine flag was wrong
        elif action == "OVERRIDE":
            target = event.get("override_disposition")
            final = target if target in DISPOSITIONS else machine
        else:                                 # ACCEPT, unknown action, or no event -> machine
            final = machine
        rows.append({
            "invoice_number": record.get("invoice_number"),
            "source_file": record.get("source_file"),
            "vendor_name": record.get("vendor_name"),
            "machine_disposition": machine,
            "reviewer_action": action,
            "final_disposition": final,
        })
    return rows


# ---------------------------------------------------------------------------- render helpers

def _dispositions_line(totals: dict) -> str:
    return " ; ".join(f"{k} {totals.get(k, 0)}" for k in _DISPOSITION_CLASSES)


def _feedback_counts(events: list[dict]) -> dict[str, int]:
    counts = {k: 0 for k in _FEEDBACK_CLASSES}
    for event in events:
        action = str(event.get("action") or "").upper()
        if action in counts:
            counts[action] += 1
    return counts


def _event_lines(events: list[dict]) -> list[str]:
    lines = []
    for event in events:
        action = str(event.get("action") or "").upper()
        if action == "OVERRIDE":
            lines.append(f"- {event.get('invoice_id')} : OVERRIDE -> "
                         f"{event.get('override_disposition')}")
        else:
            lines.append(f"- {event.get('invoice_id')} : {action}")
    return lines


def _loop_a_line(suggestion: dict) -> str:
    """One ASCII line from a feedback.recommend_thresholds suggestion (Loop A)."""
    parts = [
        f"baseline_cosine_threshold {suggestion.get('baseline_cosine_threshold')}",
        f"rejected_flags {suggestion.get('rejected_flags')}",
    ]
    for category, info in (suggestion.get("categories") or {}).items():
        parts.append(f"{category} {info.get('current')} -> {info.get('suggested')}")
    return "Loop A: " + " ; ".join(parts)


def _count_table(header: str, rows: list[tuple[str, int]]) -> list[str]:
    out = [f"| {header} | Count |", "|---|---|"]
    out.extend(f"| {key} | {value} |" for key, value in rows)
    out.append("")
    return out


# ------------------------------------------------------------------------------- console/text

def render_review_report(records: Iterable[dict], events: Iterable[dict] = (),
                         suggestion: dict | None = None) -> str:
    """Renders the before / after review brief as plain ASCII text (three labelled parts).

    Where called:
        Called by run.py in Pass 2 for the console, and in tests of the report shape.

    Where it sends information:
        Returns a string: FIRST RUN (machine counts) / FEEDBACK RECEIVED (action counts +
        one line per event) / FINAL RESULT (post-review counts + Loop A when a suggestion
        is given).
    """
    records = list(records)
    events = list(events)
    rules = "=" * 64
    lines = [rules, " INTERACTIVE REVIEW REPORT", rules, ""]

    lines.append(" FIRST RUN - machine dispositions")
    lines.append("-" * 64)
    lines.append(_dispositions_line(summarize.measured_totals(records)))
    lines.append("")

    lines.append(" FEEDBACK RECEIVED - reviewer actions")
    lines.append("-" * 64)
    counts = _feedback_counts(events)
    lines.append(" ; ".join(f"{k} {counts[k]}" for k in _FEEDBACK_CLASSES))
    if not events:
        lines.append("(no reviewer events recorded)")
    else:
        lines.extend(_event_lines(events))
    lines.append("")

    lines.append(" FINAL RESULT - dispositions after review")
    lines.append("-" * 64)
    finals: dict[str, int] = {}
    for row in final_dispositions(records, events):
        finals[row["final_disposition"]] = finals.get(row["final_disposition"], 0) + 1
    lines.append(_dispositions_line(finals))
    if suggestion:
        lines.append(_loop_a_line(suggestion))
    lines.append(rules)
    return "\n".join(lines) + "\n"


def write_review_summary(out_dir: Path | str, records: Iterable[dict],
                         events: Iterable[dict] = (), suggestion: dict | None = None) -> Path:
    """Writes `review_summary.md`: a markdown version of the three report parts.

    Where called:
        Called by run.py in Pass 2 after the review session (and in tests of the artefact).

    Where it sends information:
        Writes <out_dir>/review_summary.md (tables per part; Loop A line when a suggestion
        is given) and returns the Path. Creates out_dir if needed.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / SUMMARY_NAME

    records = list(records)
    events = list(events)
    out = ["# Interactive Review Summary", ""]

    out.append("## FIRST RUN - machine dispositions")
    out.append("")
    totals = summarize.measured_totals(records)
    out.extend(_count_table("Disposition",
                            [(k, totals.get(k, 0)) for k in _DISPOSITION_CLASSES]))

    out.append("## FEEDBACK RECEIVED - reviewer actions")
    out.append("")
    counts = _feedback_counts(events)
    out.extend(_count_table("Action", [(k, counts[k]) for k in _FEEDBACK_CLASSES]))
    if not events:
        out.append("(no reviewer events recorded)")
        out.append("")
    else:
        out.extend(_event_lines(events))
        out.append("")

    out.append("## FINAL RESULT - dispositions after review")
    out.append("")
    finals: dict[str, int] = {}
    for row in final_dispositions(records, events):
        finals[row["final_disposition"]] = finals.get(row["final_disposition"], 0) + 1
    out.extend(_count_table("Disposition", [(k, finals.get(k, 0)) for k in _DISPOSITION_CLASSES]))
    if suggestion:
        out.append(_loop_a_line(suggestion))
        out.append("")

    path.write_text("\n".join(out), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------- menu helpers

def _print_item(index: int, total: int, record: dict, prior_event: dict | None,
                print_fn: Callable[..., None]) -> None:
    """Prints one exception's reviewer-facing case sheet: tells, blockers, counterparty."""
    invoice = record.get("invoice_number")
    decision = summarize.decision_of(record)
    print_fn("=" * 72)
    print_fn(f"[{index}/{total}] {invoice} : {record.get('vendor_name')}")
    print_fn(f"    machine disposition : {summarize.disposition_of(record)}")
    print_fn(f"    risk level         : {decision.get('risk_level')}")
    print_fn(f"    reason codes       : {' ; '.join(decision.get('reason_codes') or [])}")
    for blocker in decision.get("blockers") or []:
        print_fn(f"    -> [{blocker.get('rule_id')}] {blocker.get('detail')}")
    if decision.get("next_action"):
        print_fn(f"    next action        : {decision.get('next_action')}")
    counterparty = summarize.counterparty_of(record)
    if counterparty:
        print_fn(f"    counterparty       : {counterparty}")
    if prior_event:
        action = str(prior_event.get("action") or "").upper()
        target = prior_event.get("override_disposition")
        if action == "OVERRIDE" and target in DISPOSITIONS:
            print_fn(f"    Previous review: OVERRIDE -> {target}")
        else:
            print_fn(f"    Previous review: {action}")
    print_fn("-" * 72)


def _prompt_action(invoice, input_fn: Callable[[str], str],
                   print_fn: Callable[..., None]) -> str:
    """Prompts for one review action; re-prompts on anything unrecognised (never advances)."""
    prompt = f"  action for {invoice} (ACCEPT / REJECT / OVERRIDE / SKIP / QUIT): "
    while True:
        raw = str(input_fn(prompt) or "").strip().upper()
        action = _ACTION_BY_INPUT.get(raw)
        if action:
            return action
        print_fn(f"    '{raw}' is not a review action - use ACCEPT / REJECT / OVERRIDE / SKIP / QUIT")


def _prompt_override(input_fn: Callable[[str], str], print_fn: Callable[..., None]) -> str:
    """Prompts for the OVERRIDE target disposition; re-prompts within the sub-prompt."""
    prompt = "Override to [C]lear / [F]lag-review / [H]old: "
    while True:
        raw = str(input_fn(prompt) or "").strip().upper()
        disposition = _DISPOSITION_BY_INPUT.get(raw)
        if disposition:
            return disposition
        print_fn(f"    '{raw}' is not a disposition - use C / F / H")


def _counterparty_invoice_id(record: dict) -> str | None:
    """The raw counterparty invoice id behind the matched duplicate evidence (or None).

    Reads the same evidence blocks as summarize.counterparty_of (impostor pair, flagged
    within-batch pair, historic exact/near match) but returns the bare invoice id for the
    journal event rather than the display string.
    """
    duplicates = record.get("duplicates") or {}
    verdicts = duplicates.get("impostor_verdicts") or []
    if verdicts:
        return verdicts[0].get("counterparty_invoice")
    flagged = [p for p in duplicates.get("within_batch") or [] if p.get("flagged")]
    if flagged:
        pair = flagged[0]
        invoice = record.get("invoice_number")
        return pair["b_invoice"] if pair["a_invoice"] == invoice else pair["a_invoice"]
    historic = duplicates.get("vs_historic") or {}
    exact = historic.get("exact_matches") or []
    if exact:
        return exact[0].get("invoice_id")
    near = [m for m in historic.get("near_matches") or []
            if m.get("within_tolerance") and m.get("within_window")]
    if near:
        return near[0].get("invoice_id")
    return None


def _new_event(action: str, record: dict, override: str | None) -> dict:
    event = {
        "invoice_id": record.get("invoice_number"),
        "action": action,
        "counterparty_invoice": _counterparty_invoice_id(record),
        "note": None,
    }
    if override is not None:
        event["override_disposition"] = override
    return event


def _print_loop_a_hint(events: list[dict], records: Iterable[dict],
                       print_fn: Callable[..., None]) -> None:
    """One Loop A hint when the session holds REJECTs (false positives suggest tuning).

    Pure derivation over feedback.run_telemetry_row + feedback.recommend_thresholds — a
    hint only, so any derivation error is swallowed instead of failing the review session.
    """
    if not any(str(e.get("action") or "").upper() == "REJECT" for e in events):
        return
    try:
        rows = [feedback.run_telemetry_row(r) for r in records]
        print_fn(_loop_a_line(feedback.recommend_thresholds(rows, events)))
    except Exception:
        return


# ------------------------------------------------------------------------------- menu (pure)

def run_review(records: Iterable[dict], out_dir: Path | str,
               input_fn: Callable[[str], str] = input,
               print_fn: Callable[..., None] = print,
               prior: Iterable[dict] = ()) -> list[dict]:
    """Runs the interactive exception menu; journals confirmed actions; returns all events.

    Where called:
        Called by run.py in Pass 2 after S11 outputs (and in tests, with injected
        input_fn / print_fn and a temp out_dir).

    Where it sends information:
        Walks summarize.exception_records worst-first, prints each case sheet, prompts for
        ACCEPT / REJECT / OVERRIDE / SKIP / QUIT, journals every newly captured event in
        ONE feedback.record_feedback call (also on interrupt / quit, via try/finally), and
        returns list(prior) + new_events. With no exceptions, prints a short line and
        returns list(prior).
    """
    prior_events = list(prior)
    exceptions = summarize.exception_records(records)
    if not exceptions:
        print_fn("no exceptions to review (every invoice already CLEAR)")
        return prior_events

    prior_by_invoice: dict[str, dict] = {}
    for event in prior_events:                       # append order wins here too
        prior_by_invoice[str(event.get("invoice_id"))] = event

    total = len(exceptions)
    new_events: list[dict] = []
    print_fn("=" * 72)
    print_fn(f" INTERACTIVE REVIEW - {total} exception(s), worst first"
             " - actions: ACCEPT / REJECT / OVERRIDE / SKIP / QUIT")
    print_fn("=" * 72)
    try:
        for index, record in enumerate(exceptions, 1):
            invoice = record.get("invoice_number")
            _print_item(index, total, record,
                        prior_by_invoice.get(str(invoice)), print_fn)
            action = _prompt_action(invoice, input_fn, print_fn)
            if action == "QUIT":
                print_fn(f"    quit; kept {len(new_events)} confirmed action(s)")
                break
            if action == "SKIP":
                print_fn("    skipped (no event recorded)")
                continue
            override = _prompt_override(input_fn, print_fn) if action == "OVERRIDE" else None
            event = _new_event(action, record, override)
            new_events.append(event)
            print_fn(f"    recorded {event['action']} for {invoice}"
                     + (f" -> {override}" if override else ""))
    except (EOFError, KeyboardInterrupt):
        print_fn(f"    review interrupted; kept {len(new_events)} confirmed action(s)")
    finally:
        if new_events:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            feedback.record_feedback(journal_path(out_dir), new_events)

    _print_loop_a_hint(prior_events + new_events, records, print_fn)
    return prior_events + new_events
