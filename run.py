#!/usr/bin/env python3
"""AP Intelligent Processing Pipeline — AI-primary, single command, cross-platform.

    python run.py                 # full batch (AI reads every invoice; regex corroborates)
    python run.py --no-ai         # deterministic-only demonstration of the fallback path
    python run.py --limit 3       # first N invoices (quick demo)
    python run.py --only 03788    # one invoice by filename fragment

Two passes, because duplication has two different scopes: **pass 1** extracts, verifies, links and
applies R1–R6 plus the vendor-scoped historic check (R5b) invoice by invoice; **pass 2** compares the
whole batch for semantic near-duplicates (R5a), then folds every layer into one disposition ladder
and writes the plain-language messages.

Exit codes: 0 = pipeline completed · 1 = control record failed (wrong file count) ·
            2 = extraction failure on at least one invoice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console safety (G2)

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from src.extract_ai import extract_invoice_ai, extract_invoice_fallback_regex  # noqa: E402
from src.extract_regex import ExtractionError, extract_invoice_regex, read_pdf_text  # noqa: E402
from src.duplicates import (  # noqa: E402
    Embedder,
    check_historic,
    find_within_batch_pairs,
    forensic_verdict,
    load_historic,
    pair_rule,
)
from src.enrich import enrich_record  # noqa: E402
from src.fx import fx_as_of, load_fx_table, to_eur  # noqa: E402
from src.ingest import ingest  # noqa: E402
from src.llm_provider import LLMBadOutput, LLMUnavailable, is_reachable, unload  # noqa: E402
from src.rules import disposition, reason_codes, run_rules  # noqa: E402
from src.validate_extract import verify  # noqa: E402
from src.vendor_match import load_vendor_master, match_vendor  # noqa: E402


def _json_default(obj):
    if isinstance(obj, Decimal):
        return format(obj, "f")
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def resolve_data_dir(cli_value: str | None) -> Path:
    import os
    candidates = []
    if cli_value:
        candidates.append(Path(cli_value))
    if os.environ.get("AP_DATA_DIR"):
        candidates.append(Path(os.environ["AP_DATA_DIR"]))
    candidates.append(REPO_ROOT / "ap-automation-assessment")
    candidates.append(REPO_ROOT.parent / "ap-automation-assessment")
    for cand in candidates:
        if (cand / "invoices").is_dir():
            return cand
    searched = "\n  ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"could not locate the assessment data dir (needs an 'invoices/' subfolder). Searched:\n  {searched}\n"
        "Pass --data-dir or set AP_DATA_DIR."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="AP invoice pipeline (AI-primary)")
    parser.add_argument("--data-dir", default=None, help="dir containing invoices/ + the two CSVs")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N invoices")
    parser.add_argument("--only", default=None, help="process invoices whose filename contains this fragment")
    parser.add_argument("--no-ai", action="store_true", help="skip the LLM (regex_fallback provenance demo)")
    parser.add_argument("--keep-loaded", action="store_true",
                        help="leave the model resident in RAM after the run (default: unload it)")
    args = parser.parse_args()

    config = json.loads((REPO_ROOT / "config" / "pipeline_config.json").read_text(encoding="utf-8"))
    llm_cfg = config["llm"]
    dup_cfg = config.get("duplicates", {})
    enrich_cfg = config.get("enrichment", {})
    expected = config["expected_invoice_count"]

    fx_table = load_fx_table()
    fx_as_of_date = fx_as_of()

    data_dir = resolve_data_dir(args.data_dir)
    vendor_master = load_vendor_master(data_dir / "vendor_master.csv")
    historic = load_historic(data_dir / "historic_invoices.csv")
    embedder = Embedder(dup_cfg.get("embedding_backend", "auto"))
    control = ingest(data_dir / "invoices", expected)
    print(f"AP pipeline — AI-primary extraction "
          f"(model={llm_cfg['model']}, prompt={llm_cfg['prompt_version']})")
    print(f"Control record: {control.found}/{control.expected} PDFs · unique SHA-256: "
          f"{'yes' if not control.duplicate_hashes else 'NO — DUPLICATES PRESENT'} · dir: {control.invoices_dir}")
    print(f"Reference data: {len(vendor_master)} vendors · {len(historic)} historic invoices · "
          f"similarity backend: {embedder.name}")
    if not control.ok or control.duplicate_hashes:
        return 1

    ai_available = (not args.no_ai) and is_reachable(llm_cfg["base_url"])
    if args.no_ai:
        print("AI disabled by --no-ai — running the named regex fallback path.")
    elif not ai_available:
        print(f"WARNING: Ollama unreachable at {llm_cfg['base_url']} — running regex_fallback "
              "with loud provenance (P-E01 fail-safe).")

    files = list(control.files)
    if args.only:
        files = [f for f in files if args.only in f.name]
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("nothing selected by --only/--limit")
        return 2

    records: list[dict] = []
    telemetry: list[dict] = []
    telemetry_by_file: dict[str, dict] = {}
    extraction_failures = 0

    for i, f in enumerate(files, 1):
        text = read_pdf_text(Path(control.invoices_dir) / f.name)
        try:
            corroboration = extract_invoice_regex(text, f.name)
        except ExtractionError as exc:
            print(f"[{i:2}/{len(files)}] {f.name:<44} CORROBORATION-ERROR {exc}")
            corroboration = None

        if ai_available:
            try:
                record = extract_invoice_ai(
                    text, f.name,
                    model=llm_cfg["model"],
                    base_url=llm_cfg["base_url"],
                    temperature=llm_cfg.get("temperature", 0.0),
                    prompt_version=llm_cfg.get("prompt_version", "unpinned"),
                )
            except (LLMUnavailable, LLMBadOutput) as exc:
                extraction_failures += 1
                print(f"[{i:2}/{len(files)}] {f.name:<44} AI-ERROR -> regex_fallback ({exc})")
                record = (extract_invoice_fallback_regex(text, f.name, corroboration)
                          if corroboration else None)
        else:
            record = (extract_invoice_fallback_regex(text, f.name, corroboration)
                      if corroboration else None)

        if record is None:
            extraction_failures += 1
            continue

        verify(record, corroboration)
        records.append(record)
        v = record["verification"]

        # B2 — vendor match + FX + rules. Thresholding uses ONLY the verified amount:
        # an unverified total must never be cleared against a spend threshold (R3 fails closed).
        vendor_match = match_vendor(record.get("vendor_name"), record.get("vendor_country"), vendor_master)
        amount_eur, fx_evidence = to_eur(record.get("total_trusted"), record.get("currency"),
                                         fx_table, fx_as_of_date)
        rule_results = run_rules(record, vendor_match, amount_eur)
        disp = disposition(rule_results)
        record["vendor_match"] = {
            "status": vendor_match.status,
            "vendor_id": vendor_match.vendor_id,
            "vendor_name_matched": vendor_match.vendor_name_matched,
            "fuzzy_score": vendor_match.fuzzy_score,
            "country_match": vendor_match.country_match,
            "reasons": list(vendor_match.reasons),
        }
        record["fx"] = {
            "currency": fx_evidence["currency"],
            "amount_in_currency": fx_evidence["amount"],
            "amount_eur": fx_evidence["amount_eur"],
            "rate_to_eur": fx_evidence["rate"],
            "as_of": fx_evidence["as_of"],
            "fx_known": fx_evidence["fx_known"],
        }
        record["vendor_master"] = {
            "vendor_id": vendor_match.vendor_id,
            "vendor_name_matched": vendor_match.vendor_name_matched,
            "status": (vendor_match.vendor_row.status if vendor_match.vendor_row else None),
            "category": (vendor_match.vendor_row.category if vendor_match.vendor_row else None),
            "country": (vendor_match.vendor_row.country if vendor_match.vendor_row else None),
            "approved_currency": (vendor_match.vendor_row.approved_currency if vendor_match.vendor_row else None),
            "spend_threshold_eur": (vendor_match.vendor_row.spend_threshold_eur if vendor_match.vendor_row else None),
            "requires_dual_approval": (bool(vendor_match.vendor_row.requires_dual_approval)
                                       if vendor_match.vendor_row else None),
            "payment_terms_days": (vendor_match.vendor_row.payment_terms_days
                                   if vendor_match.vendor_row else None),
            "contact_email": (vendor_match.vendor_row.contact_email if vendor_match.vendor_row else None),
        }
        record["fx"] = {
            "currency": fx_evidence["currency"],
            "amount_in_currency": fx_evidence["amount"],
            "amount_eur": fx_evidence["amount_eur"],
            "rate_to_eur": fx_evidence["rate"],
            "as_of": fx_evidence["as_of"],
            "fx_known": fx_evidence["fx_known"],
        }
        # R5b — vendor-scoped metadata match against history: evidence always, flag only inside tolerance ∧ window.
        historic_check = check_historic(
            record,
            historic,
            amount_tolerance_pct=dup_cfg.get("amount_tolerance_pct", 2.0),
            date_window_days=dup_cfg.get("date_window_days", 30),
        )
        record["duplicates"] = {"vs_historic": historic_check, "within_batch": [], "impostor_verdicts": []}
        # The disposition is provisional here: R5a is a batch layer, appended in the pass below.
        record["rules"] = {"results": rule_results + [historic_check],
                           "disposition": disposition(rule_results + [historic_check])}

        telemetry.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_file": f.name,
            "sha256": f.sha256,
            "extraction_layer": record["extraction_layer"],
            "model": record.get("model"),
            "prompt_version": record.get("prompt_version"),
            "latency_ms": record.get("latency_ms"),
            "tokens": record.get("tokens"),
            "total_agrees_with_ai": v.get("total_agrees_with_ai"),
            "line_math_ok": v.get("line_math_ok"),
            "agreement_score": v.get("agreement_score"),
            "review_flags": v.get("review_flags", []),
            "vendor_id": record["vendor_match"]["vendor_id"],
            "vendor_match_status": record["vendor_match"]["status"],
            "amount_eur": record["fx"]["amount_eur"],
            "fx_known": record["fx"]["fx_known"],
            "disposition": None,  # completed after the duplicate layer (pass 2)
            "reason_codes": [],
        })
        telemetry_by_file[f.name] = telemetry[-1]

        inv = record.get("invoice_number") or "?"
        vendor = (record.get("vendor_name") or "?")[:22]
        total = record.get("total_trusted")
        cur = record.get("currency") or "?"
        if v.get("total_status") == "wrap_resolved":
            flag = "WRAP-join(authority wins)"
        elif record.get("total_trusted") is None:
            flag = "REVIEW(witnesses disagree)"
        elif v.get("total_status") == "mismatch":
            flag = "MISMATCH(review)"
        else:
            flag = "ok"
        print(f"[{i:2}/{len(files)}] {f.name:<44} {inv:<18} {vendor:<22} "
              f"{str(total):>12} {cur:<4} math={'ok' if v['line_math_ok'] else 'FAIL'} "
              f"agr={v.get('agreement_score') or '-':>6} total={flag} "
              f"{record.get('latency_ms') or 0:>4}ms vnd={vendor_match.status} "
              f"fx={fx_evidence['fx_known']}")

    # ------------------------------------------------------------------ S7/S8: duplicate layer
    pairs = find_within_batch_pairs(
        records,
        embedder,
        cosine_threshold=dup_cfg.get("cosine_threshold", 0.85),
        quantity_match_threshold=dup_cfg.get("quantity_match_threshold", 0.95),
        evidence_floor=dup_cfg.get("evidence_floor", 0.60),
    )
    by_invoice = {str(r.get("invoice_number") or r.get("source_file")): r for r in records}
    for pair in pairs:
        # every measured pair is published on both invoices as scored evidence
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
                "rule_id": "R5a",
                "counterparty_invoice": other[0],
                "counterparty_vendor": other[1],
                "verdict": verdict,
            })
        print(f"R5a IMPOSTOR CANDIDATE: {pair.a_vendor} ↔ {pair.b_vendor} — "
              f"cosine {pair.description_cosine:.3f} · qty match {pair.quantity_match:.2f} · "
              f"prices {pair.unit_price_ratio * 100:.1f}% · "
              f"verdict {verdict['risk_level']} ({verdict['source']})")

    for record in records:
        results = record["rules"]["results"]
        record["rules"]["disposition"] = disposition(results)
        record["enrichment"] = enrich_record(
            record,
            llm_cfg=(llm_cfg if ai_available else None),
            author=enrich_cfg.get("author_messages", True),
        )
        trail = telemetry_by_file.get(record["source_file"])
        if trail is not None:
            trail["disposition"] = record["rules"]["disposition"]
            trail["reason_codes"] = reason_codes(results)
            trail["within_batch_flags"] = sum(
                1 for p in record["duplicates"]["within_batch"] if p["flagged"])
            trail["impostor_risk"] = (
                record["duplicates"]["impostor_verdicts"][0]["verdict"]["risk_level"]
                if record["duplicates"]["impostor_verdicts"] else None)
            trail["message_source"] = (record["enrichment"]["message"] or {}).get("source")

    # ---- outputs ------------------------------------------------------------
    out_dir = REPO_ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "extraction_records.json").write_text(
        json.dumps(records, default=_json_default, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with (out_dir / "ai_telemetry.jsonl").open("w", encoding="utf-8") as fh:
        for row in telemetry:
            fh.write(json.dumps(row, default=_json_default, ensure_ascii=False) + "\n")

    # ---- measured summary ---------------------------------------------------
    layers: dict[str, int] = {}
    for r in records:
        layers[r["extraction_layer"]] = layers.get(r["extraction_layer"], 0) + 1
    math_ok = sum(1 for r in records if r["verification"]["line_math_ok"])
    trusted = sum(1 for r in records if r["total_trusted"] is not None)
    wrapped = sum(1 for r in records if r["verification"]["wrap_detected"])
    review = sum(1 for r in records if r["verification"]["review_flags"])
    scores = [r["verification"].get("agreement_score") for r in records if r["verification"].get("agreement_score")]
    agreed = sum(int(s.split("/")[0]) for s in scores)
    compared = sum(int(s.split("/")[1]) for s in scores)
    latencies = [r["latency_ms"] for r in records if r.get("latency_ms")]
    mean_latency = sum(latencies) / len(latencies) if latencies else 0

    print()
    print(f"Processed: {len(records)}/{len(files)} · extraction failures: {extraction_failures}")
    print(f"Layers: {layers}")
    print(f"Arithmetic authority: trusted totals {trusted}/{len(records)} · "
          f"page-wrapped totals joined {wrapped} · line-math ok {math_ok}/{len(records)} · "
          f"records with review flags {review}")
    print(f"AI<->regex field agreement: {agreed}/{compared} "
          f"({100.0 * agreed / compared:.1f}%)" if compared else "agreement: n/a")
    if latencies:
        print(f"Mean AI latency: {mean_latency:.0f} ms/invoice")
    dispositions: dict[str, int] = {}
    for r in records:
        d = (r.get("rules") or {}).get("disposition") or "CLEAR"
        dispositions[d] = dispositions.get(d, 0) + 1
    flagged = [r for r in records if (r.get("rules") or {}).get("disposition") != "CLEAR"]

    exact_hist = sum(1 for r in records
                     if (r["duplicates"]["vs_historic"]["exact_matches"]))
    flagged_hist = sum(1 for r in records
                       if r["duplicates"]["vs_historic"]["status"] == "flag")
    clone_pairs = sum(1 for p in pairs if p.flagged)
    published_pairs = len(pairs)
    messages = sum(1 for r in records if (r.get("enrichment") or {}).get("message"))
    llm_messages = sum(1 for r in records
                       if ((r.get("enrichment") or {}).get("message") or {}).get("source") == "llm")
    note_tells = sum(len((r.get("enrichment") or {}).get("note_tells") or []) for r in records)

    print(f"Duplicate layer: historic exact {exact_hist} · historic flagged {flagged_hist} · "
          f"within-batch pairs published {published_pairs} (clone packages {clone_pairs}) · "
          f"backend {embedder.name}")
    for pair in pairs:
        mark = "FLAG" if pair.flagged else "evid"
        print(f"  {mark} {pair.a_vendor[:20]:<20} ↔ {pair.b_vendor[:20]:<20} "
              f"cos={pair.description_cosine:.3f} qty={pair.quantity_match:.2f} "
              f"price={('%.1f%%' % (pair.unit_price_ratio * 100)) if pair.unit_price_ratio else 'n/a':>7} "
              f"{pair.pattern}")
    print(f"Enrichment: {messages} authored messages ({llm_messages} LLM · "
          f"{messages - llm_messages} template) · {note_tells} informational note tells")
    print(f"Dispositions: {dispositions}")
    for r in flagged:
        reasons = reason_codes((r.get("rules") or {}).get("results") or [])
        message = ((r.get("enrichment") or {}).get("message") or {})
        print(f"  {r.get('invoice_number') or r.get('source_file')}: "
              f"{r.get('rules', {}).get('disposition')} — {'; '.join(reasons)}")
        if message:
            print(f"      → {message.get('channel')} note ({message.get('source')}): "
                  f"{message.get('next_action')}")
    print(f"Wrote: output/extraction_records.json · output/ai_telemetry.jsonl")

    # ---- free the model from RAM the moment computation stops (directive) ----
    if ai_available and not args.keep_loaded:
        if unload(llm_cfg["model"], llm_cfg["base_url"]):
            print(f"LLM unloaded from RAM: {llm_cfg['model']} "
                  "(pass --keep-loaded to keep it resident)")

    return 2 if extraction_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
