#!/usr/bin/env python3
"""AP Intelligent Processing Pipeline — AI-primary, single command, cross-platform.

    python run.py                 # full batch (AI reads every invoice; regex corroborates)
    python run.py --no-ai         # deterministic-only demonstration of the fallback path
    python run.py --limit 3       # first N invoices (quick demo)
    python run.py --only 03788    # one invoice by filename fragment

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
from src.ingest import ingest  # noqa: E402
from src.llm_provider import LLMBadOutput, LLMUnavailable, is_reachable, unload  # noqa: E402
from src.validate_extract import verify  # noqa: E402


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
    expected = config["expected_invoice_count"]

    data_dir = resolve_data_dir(args.data_dir)
    control = ingest(data_dir / "invoices", expected)
    print(f"AP pipeline — AI-primary extraction "
          f"(model={llm_cfg['model']}, prompt={llm_cfg['prompt_version']})")
    print(f"Control record: {control.found}/{control.expected} PDFs · unique SHA-256: "
          f"{'yes' if not control.duplicate_hashes else 'NO — DUPLICATES PRESENT'} · dir: {control.invoices_dir}")
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
        })

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
              f"{record.get('latency_ms') or 0}ms")

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
    print(f"Wrote: output/extraction_records.json · output/ai_telemetry.jsonl")

    # ---- free the model from RAM the moment computation stops (directive) ----
    if ai_available and not args.keep_loaded:
        if unload(llm_cfg["model"], llm_cfg["base_url"]):
            print(f"LLM unloaded from RAM: {llm_cfg['model']} "
                  "(pass --keep-loaded to keep it resident)")

    return 2 if extraction_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
