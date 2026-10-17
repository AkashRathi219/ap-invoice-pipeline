#!/usr/bin/env python3
"""AP Intelligent Processing Pipeline — AI-primary, single command, cross-platform.

    python run.py                          # full batch (AI reads every invoice; regex corroborates)
    python run.py --no-ai                  # deterministic regex_fallback path (no LLM calls)
    python run.py --limit 5                # first N invoices
    python run.py --only 03760             # invoices whose filename contains this fragment
    python run.py --out-dir sample_output  # write the artefacts somewhere else

The pipeline runs the full S1–S11 chain: ingest → extract → verify → vendor/FX/rules →
R5 duplicates (metadata + semantic) → S9 enrichment → **S10 decide** → **S11 outputs**.
AI proposes (reads, scores, explains); the deterministic ladder and the arithmetic authority
dispose. Every AI-touching call has a named offline fallback, so the pipeline always completes.

Exit codes: 0 ok · 1 control-record failure (wrong file count / duplicate hashes) · 2 no invoice
could be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src import decide as decide_mod
from src import feedback, review, summarize
from src.duplicates import (
    Embedder,
    check_historic,
    find_within_batch_pairs,
    forensic_verdict,
    load_historic,
    pair_rule,
)
from src.enrich import enrich_record
from src.extract_ai import extract_invoice_ai, extract_invoice_fallback_regex
from src.extract_regex import ExtractionError, extract_invoice_regex, read_pdf_text
from src.fx import fx_as_of, load_fx_table, to_eur
from src.ingest import ingest
from src.llm_provider import LLMBadOutput, LLMUnavailable, is_reachable, unload
from src.rules import disposition, run_rules
from src.validate_extract import verify
from src.vendor_match import load_vendor_master, match_vendor

REPO_ROOT = Path(__file__).resolve().parent


def _json_default(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"not JSON-serialisable: {type(value)!r}")


def load_config() -> dict:
    """Reads config/pipeline_config.json (module-level seam so tests can pin a batch size)."""
    return json.loads((REPO_ROOT / "config" / "pipeline_config.json").read_text(encoding="utf-8"))


def resolve_data_dir(data_dir: str | None = None) -> Path:
    """Locate the assessment data dir (invoices/ + the two CSVs), cross-platform."""
    import os
    candidates = []
    if data_dir:
        candidates.append(Path(data_dir))
    if os.environ.get("AP_DATA_DIR"):
        candidates.append(Path(os.environ["AP_DATA_DIR"]))
    candidates += [
        REPO_ROOT / "ap-automation-assessment",
        REPO_ROOT / "input",
        REPO_ROOT.parent / "ap-automation-assessment",
    ]
    for candidate in candidates:
        if (candidate / "invoices").is_dir() and (candidate / "vendor_master.csv").is_file():
            return candidate
    searched = "\n  ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        "could not find the assessment data dir (needs invoices/ + vendor_master.csv).\n"
        f"Searched:\n  {searched}\n"
        "Copy the starter pack to ./input, pass --data-dir, or set AP_DATA_DIR."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AP invoice pipeline (AI-primary)")
    parser.add_argument("--data-dir", default=None, help="dir containing invoices/ + the two CSVs")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "output"),
                        help="where the artefacts are written (default: ./output)")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N invoices")
    parser.add_argument("--only", default=None, help="invoices whose filename contains this fragment")
    parser.add_argument("--no-ai", action="store_true", help="skip the LLM (regex_fallback demo)")
    parser.add_argument("--keep-loaded", action="store_true", help="do not unload the model at the end")
    parser.add_argument("--review", action="store_true", help="open the interactive terminal review screen (auto-skipped when stdin is not a TTY)")
    args = parser.parse_args(argv)

    cfg = load_config()
    llm_cfg = cfg["llm"]
    dup_cfg = cfg.get("duplicates", {})
    decide_cfg = cfg.get("decide", {})
    enrich_cfg = cfg.get("enrichment", {})
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data_dir = resolve_data_dir(args.data_dir)
    invoices_dir = data_dir / "invoices"
    raw_expected = cfg.get("expected_invoice_count")
    expected = int(raw_expected) if raw_expected else None
    control = ingest(invoices_dir, expected)
    mode_note = (f"{control.mode} (expected_invoice_count={control.expected})" if control.pinned
                 else f"{control.mode} (no truncation control)")
    print(f"Control record: {control.found}/{control.expected} PDFs · unique SHA-256: "
          f"{'yes' if not control.duplicate_hashes else 'NO — DUPLICATES PRESENT'} · "
          f"control mode: {mode_note} · dir: {control.invoices_dir}")
    if not control.ok or control.duplicate_hashes:
        return 1

    files = list(control.files)
    if args.only:
        files = [f for f in files if args.only in f.name]
    if args.limit:
        files = files[: args.limit]
    if not files:
        if not control.files:
            print(f"no PDFs found in {control.invoices_dir} — nothing to process")
        else:
            print("nothing to process after --only/--limit")
        return 2

    ai_available = (not args.no_ai) and is_reachable(llm_cfg["base_url"])
    mode = ("AI-primary (llama3.2:3b)" if ai_available
            else "regex_fallback (--no-ai)" if args.no_ai else "regex_fallback (Ollama unreachable)")
    print(f"AP invoice pipeline · {len(files)} invoice(s) · extraction: {mode} "
          f"· prompt: {llm_cfg['prompt_version']}")

    historic = load_historic(data_dir / "historic_invoices.csv")
    vendor_master = load_vendor_master(data_dir / "vendor_master.csv")
    fx_table = load_fx_table()
    fx_as_of_date = fx_as_of()
    embedder = Embedder(dup_cfg.get("embedding_backend", "auto"))

    records: list[dict] = []
    for i, pdf in enumerate(files, 1):
        text = read_pdf_text(Path(control.invoices_dir) / pdf.name)
        try:
            corroboration = extract_invoice_regex(text, pdf.name)
        except ExtractionError as exc:
            corroboration = None
            print(f"[{i:2}/{len(files)}] {pdf.name:<48} regex corroboration failed: {exc}")

        record = None
        if ai_available:
            try:
                record = extract_invoice_ai(text, pdf.name, model=llm_cfg["model"],
                                            base_url=llm_cfg["base_url"],
                                            temperature=llm_cfg["temperature"],
                                            prompt_version=llm_cfg["prompt_version"])
            except (LLMUnavailable, LLMBadOutput) as exc:
                print(f"[{i:2}/{len(files)}] {pdf.name:<48} AI failed ({exc}) -> regex_fallback")
        if record is None and corroboration is not None:
            record = extract_invoice_fallback_regex(text, pdf.name, corroboration)
        if record is None:
            print(f"[{i:2}/{len(files)}] {pdf.name:<48} could not be extracted - skipped")
            continue

        verify(record, corroboration)
        record["sha256"] = pdf.sha256
        record["format_tag"] = (corroboration or {}).get("format_tag")
        record["extracted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        vendor_match = match_vendor(record.get("vendor_name"), record.get("vendor_country"),
                                    vendor_master)
        amount_eur, fx_evidence = to_eur(record.get("total_trusted"), record.get("currency"),
                                         fx_table, fx_as_of_date)
        record["vendor_match"] = {
            "status": vendor_match.status, "vendor_id": vendor_match.vendor_id,
            "vendor_name_matched": vendor_match.vendor_name_matched,
            "fuzzy_score": vendor_match.fuzzy_score,
            "country_match": vendor_match.country_match, "reasons": list(vendor_match.reasons),
        }
        row = vendor_match.vendor_row
        record["vendor_master"] = {
            "vendor_id": vendor_match.vendor_id,
            "vendor_name_matched": vendor_match.vendor_name_matched,
            "status": row.status if row else None, "category": row.category if row else None,
            "country": row.country if row else None,
            "approved_currency": row.approved_currency if row else None,
            "spend_threshold_eur": row.spend_threshold_eur if row else None,
            "requires_dual_approval": bool(row.requires_dual_approval) if row else None,
        }
        record["fx"] = {
            "currency": fx_evidence["currency"],
            "amount_in_currency": fx_evidence["amount"],
            "amount_eur": fx_evidence["amount_eur"],
            "rate_to_eur": fx_evidence["rate"],
            "as_of": fx_evidence["as_of"],
            "fx_known": fx_evidence["fx_known"],
        }
        rule_results = run_rules(record, vendor_match, fx_evidence["amount_eur"])
        record["rules"] = {"results": rule_results, "disposition": disposition(rule_results)}

        historic_result = check_historic(
            record, historic,
            amount_tolerance_pct=dup_cfg.get("amount_tolerance_pct", 2.0),
            date_window_days=dup_cfg.get("date_window_days", 30),
        )
        record["rules"]["results"].append(historic_result)
        record["duplicates"] = {"vs_historic": historic_result, "within_batch": [],
                                "impostor_verdicts": []}
        records.append(record)
        print(f"[{i:2}/{len(files)}] {pdf.name:<48} {record.get('invoice_number')} "
              f"· {record.get('vendor_name')} · {record.get('currency')} "
              f"{record.get('total_trusted') or '(unverified)'}")

    if not records:
        print("no invoices could be read")
        return 2

    # -------------------------------------------------------- S7/S8: batch duplicate layer
    pairs = find_within_batch_pairs(
        records, embedder,
        cosine_threshold=dup_cfg.get("cosine_threshold", 0.85),
        quantity_match_threshold=dup_cfg.get("quantity_match_threshold", 0.95),
        evidence_floor=dup_cfg.get("evidence_floor", 0.60),
    )
    by_invoice = {str(r.get("invoice_number") or r.get("source_file")): r for r in records}
    for pair in pairs:
        for label in (pair.a_invoice, pair.b_invoice):
            target = by_invoice.get(label)
            if target is not None:
                target["duplicates"]["within_batch"].append(pair.to_dict())
        if not pair.flagged:
            continue
        record_a, record_b = by_invoice.get(pair.a_invoice), by_invoice.get(pair.b_invoice)
        if record_a is None or record_b is None:
            continue
        verdict = forensic_verdict(pair, record_a, record_b, llm_cfg if ai_available else None)
        rule = pair_rule(pair)
        for record, other in ((record_a, (pair.b_invoice, pair.b_vendor)),
                              (record_b, (pair.a_invoice, pair.a_vendor))):
            record["rules"]["results"].append(rule)
            record["duplicates"]["impostor_verdicts"].append({
                "rule_id": "R5a", "counterparty_invoice": other[0],
                "counterparty_vendor": other[1], "verdict": verdict,
            })

    # -------------------------------------------------------- S9: enrichment messages
    for record in records:
        record["rules"]["disposition"] = disposition(record["rules"]["results"])
        record["enrichment"] = enrich_record(record, llm_cfg if ai_available else None,
                                             author=enrich_cfg.get("author_messages", True))

    # -------------------------------------------------------- S10: one decision per invoice
    rationale_cfg = llm_cfg if (ai_available and decide_cfg.get("ai_rationale", True)) else None
    decide_mod.decide_all(records, rationale_cfg)

    # -------------------------------------------------------- S11: outputs + feedback loops
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": llm_cfg["model"], "prompt_version": llm_cfg["prompt_version"],
        "temperature": llm_cfg["temperature"], "backend": embedder.name,
        "ai_available": ai_available,
        "control": {"found": control.found, "expected": control.expected,
                    "invoices_dir": control.invoices_dir, "processed": len(records)},
    }
    (out_dir / "extraction_records.json").write_text(
        json.dumps(records, default=_json_default, indent=2, ensure_ascii=False), encoding="utf-8")
    summarize.build_outputs(records, meta, out_dir)

    # Human-in-the-loop review: additive overlay. Machine artefacts above remain the immutable
    # first-run record; reviewer actions live in a separate append-only journal.
    events = review.load_events(out_dir)
    if args.review:
        if sys.stdin.isatty():
            events = review.run_review(records, out_dir, prior=events)
        else:
            print("\n--review requested but stdin is not an interactive terminal - "
                  "skipping the review screen (machine results above are unaffected).")

    telemetry_rows = [feedback.run_telemetry_row(r) for r in records]
    feedback.write_all(records, out_dir, out_dir, telemetry_rows=telemetry_rows, feedback=events)
    suggestion = feedback.recommend_thresholds(telemetry_rows, events)

    # -------------------------------------------------------- console summary
    totals = summarize.measured_totals(records)
    oracle_result = summarize.verify_oracle(records)
    exceptions = summarize.exception_records(records)
    trusted = sum(1 for r in records if r.get("total_trusted") is not None)
    flagged_pairs = [p for p in pairs if p.flagged]
    llm_messages = sum(1 for r in records
                       if ((r.get("enrichment") or {}).get("message") or {}).get("source") == "llm")

    print()
    print("=" * 95)
    print(" BATCH EXECUTION & DISPOSITION SUMMARY")
    print("=" * 95)
    print(f"Records Ingested : {len(records)} | Verified Totals: {trusted}/{len(records)} | "
          f"Within-Batch Pairs: {len(pairs)} ({len(flagged_pairs)} flagged)")
    print(f"Dispositions     : CLEAR {totals.get('CLEAR', 0)} · "
          f"FLAG-REVIEW {totals.get('FLAG-REVIEW', 0)} · HOLD {totals.get('HOLD', 0)}")
    # N/A carries ok True: the status is tested before the ok branch so a custom dataset
    # never receives the mismatch/missing listings.
    if oracle_result["status"] == "N/A":
        print("Oracle Status    : N/A — custom dataset (no overlap with the §5.3 assessment set)")
    elif oracle_result["ok"]:
        expected = oracle_result["expected_totals"]
        print(f"Oracle Status    : MATCH (CLEAR {expected.get('CLEAR')} · "
              f"FLAG-REVIEW {expected.get('FLAG-REVIEW')} · HOLD {expected.get('HOLD')})")
    else:
        print(f"Oracle Status    : DIFF — measured {oracle_result['measured_totals']} vs expected "
              f"{oracle_result['expected_totals']}")
        for mismatch in oracle_result["mismatches"]:
            print(f"    Mismatch -> {mismatch['invoice_number']}: expected {mismatch['expected']}, "
                  f"measured {mismatch['measured']}")
        if oracle_result["missing_invoices"]:
            print(f"    Not processed: {', '.join(oracle_result['missing_invoices'])}")

    print()
    print("=" * 95)
    print(" PRIORITISED EXCEPTION QUEUE & REASON CODES")
    print("=" * 95)
    print(f"{'#':<3} {'STATUS':<12} {'INVOICE':<19} {'VENDOR':<24} {'REASON CODES'}")
    print("-" * 95)
    for idx, record in enumerate(exceptions, 1):
        decision = summarize.decision_of(record)
        reasons_str = "; ".join(decision.get("reason_codes") or [])
        print(f"{idx:<3} {str(decision.get('disposition')):<12} {str(record.get('invoice_number')):<19} "
              f"{str(record.get('vendor_name')):<24} {reasons_str}")
        
        # Interactive diagnostics breakdown per exception
        blockers = decision.get("blockers") or []
        for b in blockers:
            print(f"    -> [{b.get('rule_id')}] {b.get('detail')}")
        if decision.get("next_action"):
            print(f"    -> Action: {decision.get('next_action')}")
        counter = summarize.counterparty_of(record)
        if counter:
            print(f"    -> Matched Counterparty: {counter}")
        print()

    # Aggregate Root-Cause Error Summary
    error_counts: dict[str, int] = {}
    for record in exceptions:
        decision = summarize.decision_of(record)
        for code in decision.get("reason_codes") or []:
            error_counts[code] = error_counts.get(code, 0) + 1

    print("=" * 95)
    print(" ROOT-CAUSE ERROR & EXCEPTION DISTRIBUTION")
    print("=" * 95)
    for code, count in sorted(error_counts.items(), key=lambda x: -x[1]):
        rule_desc = {
            "R1_unknown_vendor": "Vendor not found in vendor master (unapproved supplier)",
            "R1_inactive_vendor": "Vendor marked INACTIVE in master record",
            "R3_amount_unavailable": "Dual-read disagreement / unverified numeric total",
            "R4_dual_approval_required": "Vendor spend policy mandates secondary sign-off",
            "R5a_clone_package": "Cross-invoice duplicate/impostor service clone detected",
            "R5b_exact_duplicate": "Invoice ID or PO overlap with 12-month historical data",
            "R5b_near_duplicate": "Near-identical amount & date proximity in historical data",
            "R6_po_missing": "Mandatory PO reference missing on document",
        }.get(code, "Business rule violation")
        print(f"  * {code:<28} : {count} invoice(s) -- {rule_desc}")
    print("-" * 95)

    if events:
        print()
        print(review.render_review_report(records, events, suggestion))
        review.write_review_summary(out_dir, records, events, suggestion)

    print()
    print(f"Wrote Artefacts to: {out_dir}")
    keys = ["summary.md", "exceptions.csv", "invoices_processed.csv", "report.html",
            "batch_report.json", "audit_log.jsonl", "ai_telemetry.jsonl",
            "fine_tuning_dataset.jsonl", "few_shot_bank.json", "threshold_suggestions.json"]
    # the review artefacts only exist once a review session wrote them into this out_dir
    keys += [name for name in (review.JOURNAL_NAME, review.SUMMARY_NAME)
             if (out_dir / name).exists()]
    for key in keys:
        print(f"  - {key}")

    if ai_available and not args.keep_loaded and unload(llm_cfg["model"], llm_cfg["base_url"]):
        print(f"\nModel unloaded from memory: {llm_cfg['model']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
