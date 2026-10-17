"""S11 — Outputs: the reviewer-facing artefacts, and the oracle check that guards them.

Nothing here decides anything. `decide.py` owns the ladder; this module only *renders* it into:

* `invoices_processed.csv` — one row per invoice: fields, provenance, rules, disposition.
* `exceptions.csv` — the prioritised exception queue (HOLD first, then by risk).
* `summary.md` — the plain-language brief for the AP lead.
* `batch_report.json` — the structured per-step envelopes (PIPELINE_PLAN §2).
* `audit_log.jsonl` — the immutable trail: hashes, model/prompt, provenance, decisions.
* `report.html` — a self-contained, no-CDN HTML report (from `report_template.html`).

It also reconciles the measured dispositions against the committed oracle
(`config/oracle_dispositions.json`) and reports the outcome — the golden-file check at runtime,
so a fresh clone reproduces the same reviewer-facing difference the summary claims.
"""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .decide import decide_record

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORACLE_PATH = REPO_ROOT / "config" / "oracle_dispositions.json"
TEMPLATE_PATH = Path(__file__).resolve().parent / "report_template.html"

DISPOSITION_ORDER = {"HOLD": 0, "FLAG-REVIEW": 1, "CLEAR": 2}
RISK_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

INVOICE_COLUMNS = [
    "invoice_number", "source_file", "vendor_name", "vendor_id", "vendor_match", "country",
    "invoice_date", "po_number", "currency", "subtotal", "vat_amount", "total",
    "total_trusted", "amount_eur", "fx_rate", "extraction_layer", "format_tag",
    "agreement_score", "line_math_ok", "disposition", "queue", "risk_level",
    "reason_codes", "variant_tells", "next_action",
]

EXCEPTION_COLUMNS = [
    "priority", "invoice_number", "vendor_name", "disposition", "risk_level", "queue",
    "reason_codes", "rules_fired", "evidence", "next_action", "message_channel",
    "message_subject", "counterparty",
]


# --------------------------------------------------------------------------------- helpers

def decision_of(record: dict) -> dict:
    """The decision for this record — computed on demand if the caller did not run S10."""
    decision = record.get("decision")
    if not decision:
        decision = decide_record(record)
    return decision


def disposition_of(record: dict) -> str:
    return decision_of(record).get("disposition") or "CLEAR"


def _amount_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else format(value, "f")


def _money(value) -> str:
    """Human-readable amount with thousand separators (display only, never re-parsed)."""
    text = _amount_text(value)
    if not text:
        return ""
    try:
        number = Decimal(text)
    except Exception:
        return text
    return f"{number:,.2f}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blockers(record: dict) -> list[dict]:
    return decision_of(record).get("blockers") or []


def _rules_fired(record: dict) -> list[str]:
    return [b.get("reason_code") or b.get("rule_id") for b in _blockers(record)]


def _evidence_text(record: dict) -> str:
    """Compact one-line evidence roll-up for the CSV: what the decision actually rests on."""
    evidence = decision_of(record).get("evidence") or {}
    parts: list[str] = []
    vm = evidence.get("vendor_match") or {}
    fuzzy = "" if vm.get("fuzzy_score") in (None, 1, 1.0) else f" ({vm.get('fuzzy_score')})"
    parts.append(f"vendor {vm.get('status')}{fuzzy}")
    fx = evidence.get("fx") or {}
    rate = fx.get("rate_to_eur")
    if fx.get("amount_eur") is None:
        parts.append("amount unverified")
    elif rate and str(rate) != "1.0":
        parts.append(f"FX {rate} -> {_money(fx.get('amount_eur'))} EUR")
    else:
        parts.append(f"EUR {_money(fx.get('amount_eur'))}")
    if evidence.get("threshold_eur"):
        parts.append(f"threshold {_money(evidence['threshold_eur'])}")
    arithmetic = evidence.get("arithmetic") or {}
    parts.append(f"total {arithmetic.get('total_status')}")
    dup = evidence.get("duplicates") or {}
    if dup.get("impostor_risk"):
        parts.append(f"impostor risk {dup['impostor_risk']}")
    if dup.get("historic_reason"):
        parts.append(f"historic {dup['historic_reason']}")
    if dup.get("within_batch_published"):
        parts.append(f"{dup['within_batch_published']} within-batch score(s)")
    return " | ".join(parts)


def _counterparty(record: dict) -> str:
    verdicts = (record.get("duplicates") or {}).get("impostor_verdicts") or []
    if verdicts:
        return f"{verdicts[0].get('counterparty_invoice')} ({verdicts[0].get('counterparty_vendor')})"
    within = (record.get("duplicates") or {}).get("within_batch") or []
    flagged = [p for p in within if p.get("flagged")]
    if flagged:
        pair = flagged[0]
        invoice = record.get("invoice_number")
        return pair["b_invoice"] if pair["a_invoice"] == invoice else pair["a_invoice"]
    return ""


def sort_key(record: dict):
    decision = decision_of(record)
    return (DISPOSITION_ORDER.get(decision.get("disposition"), 9),
            RISK_ORDER.get(decision.get("risk_level"), 4),
            str(record.get("invoice_number") or record.get("source_file") or ""))


def exception_records(records: Iterable[dict]) -> list[dict]:
    """Non-CLEAR invoices, prioritised: HOLD first, then FLAG-REVIEW, worst risk first."""
    return sorted([r for r in records if disposition_of(r) != "CLEAR"], key=sort_key)


# ----------------------------------------------------------------------------------- oracle

def load_oracle(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_ORACLE_PATH).read_text(encoding="utf-8"))


def measured_totals(records: Iterable[dict]) -> dict:
    totals: dict[str, int] = {}
    for record in records:
        disposition = disposition_of(record)
        totals[disposition] = totals.get(disposition, 0) + 1
    return totals


def verify_oracle(records: Iterable[dict], oracle: dict | None = None) -> dict:
    """Compare measured dispositions against the committed §5.3 oracle, invoice by invoice."""
    oracle = oracle or load_oracle()
    expected = oracle.get("totals") or {}
    expected_by_invoice = {k: v.get("expected") for k, v in (oracle.get("invoices") or {}).items()}
    records = list(records)
    measured = measured_totals(records)
    mismatches: list[dict] = []
    seen: set[str] = set()
    for record in records:
        invoice = str(record.get("invoice_number") or "")
        seen.add(invoice)
        want = expected_by_invoice.get(invoice)
        got = disposition_of(record)
        if want and want != got:
            mismatches.append({"invoice_number": invoice, "expected": want, "measured": got})
    missing = sorted(set(expected_by_invoice) - seen)
    totals_ok = all(measured.get(k, 0) == v for k, v in expected.items())
    return {
        "ok": totals_ok and not mismatches and not missing,
        "expected_totals": expected,
        "measured_totals": measured,
        "mismatches": mismatches,
        "missing_invoices": missing,
        "source": oracle.get("source"),
    }


# ----------------------------------------------------------------------------------- writers

def write_invoices_csv(records: Iterable[dict], path: Path | str) -> Path:
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=INVOICE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            decision = decision_of(record)
            vendor_match = record.get("vendor_match") or {}
            verification = record.get("verification") or {}
            writer.writerow({
                "invoice_number": record.get("invoice_number"),
                "source_file": record.get("source_file"),
                "vendor_name": record.get("vendor_name"),
                "vendor_id": vendor_match.get("vendor_id"),
                "vendor_match": vendor_match.get("status"),
                "country": record.get("vendor_country"),
                "invoice_date": record.get("invoice_date"),
                "po_number": record.get("po_number"),
                "currency": record.get("currency"),
                "subtotal": _amount_text(record.get("subtotal")),
                "vat_amount": _amount_text(record.get("vat_amount")),
                "total": _amount_text(record.get("total")),
                "total_trusted": _amount_text(record.get("total_trusted")),
                "amount_eur": _amount_text((record.get("fx") or {}).get("amount_eur")),
                "fx_rate": (record.get("fx") or {}).get("rate_to_eur"),
                "extraction_layer": record.get("extraction_layer"),
                "format_tag": record.get("format_tag"),
                "agreement_score": verification.get("agreement_score"),
                "line_math_ok": verification.get("line_math_ok"),
                "disposition": decision.get("disposition"),
                "queue": decision.get("queue"),
                "risk_level": decision.get("risk_level"),
                "reason_codes": ";".join(decision.get("reason_codes") or []),
                "variant_tells": " ".join(t.get("variant_id") for t in decision.get("variant_tells") or []),
                "next_action": decision.get("next_action") or "",
            })
    return path


def write_exceptions_csv(records: Iterable[dict], path: Path | str) -> Path:
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXCEPTION_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for priority, record in enumerate(exception_records(records), 1):
            decision = decision_of(record)
            message = (record.get("enrichment") or {}).get("message") or {}
            writer.writerow({
                "priority": priority,
                "invoice_number": record.get("invoice_number"),
                "vendor_name": record.get("vendor_name"),
                "disposition": decision.get("disposition"),
                "risk_level": decision.get("risk_level") or "",
                "queue": decision.get("queue"),
                "reason_codes": ";".join(decision.get("reason_codes") or []),
                "rules_fired": "; ".join(_rules_fired(record)),
                "evidence": _evidence_text(record),
                "next_action": decision.get("next_action") or "",
                "message_channel": message.get("channel") or "",
                "message_subject": message.get("subject") or "",
                "counterparty": _counterparty(record),
            })
    return path


def write_audit_log(records: Iterable[dict], meta: dict, path: Path | str) -> Path:
    path = Path(path)
    records = list(records)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "event": "RUN_START",
            "generated_at": meta.get("generated_at") or _iso_now(),
            "model": meta.get("model"),
            "prompt_version": meta.get("prompt_version"),
            "temperature": meta.get("temperature"),
            "similarity_backend": meta.get("backend"),
            "ai_available": meta.get("ai_available"),
            "control_record": meta.get("control"),
            "oracle_check": meta.get("oracle_check"),
        }, ensure_ascii=False) + "\n")
        for record in records:
            decision = decision_of(record)
            verification = record.get("verification") or {}
            message = (record.get("enrichment") or {}).get("message") or {}
            fh.write(json.dumps({
                "event": "INVOICE_DECIDED",
                "invoice_number": record.get("invoice_number"),
                "source_file": record.get("source_file"),
                "sha256": record.get("sha256"),
                "extraction_layer": record.get("extraction_layer"),
                "provenance": {
                    "model": record.get("model"),
                    "prompt_version": record.get("prompt_version"),
                    "tokens": record.get("tokens"),
                    "agreement_score": verification.get("agreement_score"),
                },
                "disposition": decision.get("disposition"),
                "queue": decision.get("queue"),
                "risk_level": decision.get("risk_level"),
                "reason_codes": decision.get("reason_codes"),
                "variant_tells": [t.get("variant_id") for t in decision.get("variant_tells") or []],
                "rationale_source": (decision.get("rationale") or {}).get("source"),
                "message_source": message.get("source"),
            }, ensure_ascii=False) + "\n")
    return path


def _envelope(record: dict) -> dict:
    decision = decision_of(record)
    within_batch = (record.get("duplicates") or {}).get("within_batch") or []
    return {
        "step_id": "S10",
        "phase": "DECIDE",
        "invoice_id": record.get("invoice_number") or record.get("source_file"),
        "status": "DONE",
        "record": {
            "P1_vendor": record.get("vendor_name"),
            "P3_invoice_number": record.get("invoice_number"),
            "P4_date": record.get("invoice_date"),
            "P6_po_number": record.get("po_number"),
            "P7_subtotal": _amount_text(record.get("subtotal")),
            "P8_vat": _amount_text(record.get("vat_amount")),
            "P9_total": _amount_text(record.get("total")),
            "P11_trusted_total": _amount_text(record.get("total_trusted")),
            "P12_currency": record.get("currency"),
            "P14_format": record.get("format_tag"),
        },
        "payload": {
            "disposition": decision.get("disposition"),
            "queue": decision.get("queue"),
            "review_required": decision.get("review_required"),
            "reason_codes": decision.get("reason_codes"),
            "decision_reasons": decision.get("decision_reasons"),
            "risk_level": decision.get("risk_level"),
        },
        "variant_tells": decision.get("variant_tells"),
        "runtime_tells": {
            "model": record.get("model"),
            "prompt_version": record.get("prompt_version"),
            "extraction_layer": record.get("extraction_layer"),
            "similarity_backend": within_batch[0].get("embedding_backend") if within_batch else None,
        },
    }


def write_batch_report(records: Iterable[dict], meta: dict, oracle_result: dict,
                       path: Path | str) -> Path:
    path = Path(path)
    records = list(records)
    payload = {
        "step_id": "S11",
        "phase": "OUTPUTS",
        "generated_at": meta.get("generated_at") or _iso_now(),
        "run": {
            "model": meta.get("model"),
            "prompt_version": meta.get("prompt_version"),
            "temperature": meta.get("temperature"),
            "similarity_backend": meta.get("backend"),
            "ai_available": meta.get("ai_available"),
            "control_record": meta.get("control"),
        },
        "dispositions": measured_totals(records),
        "oracle": oracle_result,
        "invoices": [_envelope(r) for r in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# -------------------------------------------------------------------------------------- HTML

def _pill(disposition: str) -> str:
    return f'<span class="pill {html.escape(disposition)}">{html.escape(disposition)}</span>'


def _kpi_cards(records: list[dict]) -> str:
    totals = measured_totals(records)
    trusted = sum(1 for r in records if r.get("total_trusted") is not None)
    cards = "".join(
        f'<div class="card {css}"><div class="n">{totals.get(name, 0)}</div>'
        f'<div class="l">{name}</div></div>'
        for name, css in (("CLEAR", "clear"), ("FLAG-REVIEW", "flag"), ("HOLD", "hold"))
    )
    cards += (f'<div class="card"><div class="n">{trusted}/{len(records)}</div>'
              f'<div class="l">verified totals</div></div>')
    return cards


def _exceptions_table(records: list[dict]) -> str:
    exceptions = exception_records(records)
    if not exceptions:
        return ('<tr><td colspan="6" class="muted">'
                'No exceptions — every invoice cleared.</td></tr>')
    rows = []
    for record in exceptions:
        decision = decision_of(record)
        why = "; ".join(decision.get("decision_reasons") or [])
        risk = decision.get("risk_level") or ""
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(record.get('invoice_number') or record.get('source_file')))}</code></td>"
            f"<td>{html.escape(str(record.get('vendor_name') or ''))}</td>"
            f"<td>{_pill(str(decision.get('disposition') or ''))}</td>"
            f'<td class="risk-{html.escape(risk)}">{html.escape(risk)}</td>'
            f'<td class="why">{html.escape(why)}</td>'
            f'<td class="why">{html.escape(str(decision.get("next_action") or ""))}</td>'
            "</tr>"
        )
    return "\n".join(rows)


def _clears_table(records: list[dict]) -> str:
    clears = sorted([r for r in records if disposition_of(r) == "CLEAR"],
                    key=lambda r: str(r.get("invoice_number") or ""))
    rows = []
    for record in clears:
        vm = record.get("vendor_match") or {}
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(record.get('invoice_number') or record.get('source_file')))}</code></td>"
            f"<td>{html.escape(str(record.get('vendor_name') or ''))}</td>"
            f"<td>{html.escape(str(vm.get('status') or ''))}</td>"
            f'<td class="num">{html.escape(_money(record.get("total")))}</td>'
            f'<td class="num">{html.escape(_money(record.get("total_trusted")))}</td>'
            f"<td>{_pill('CLEAR')}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _duplicate_pairs(records: list[dict]) -> list[dict]:
    pairs: list[dict] = []
    seen: set[tuple] = set()
    for record in records:
        for pair in (record.get("duplicates") or {}).get("within_batch") or []:
            key = (pair.get("a_invoice"), pair.get("b_invoice"))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(pair)
    return pairs


def _ratio_pct(ratio) -> str:
    return f"{float(ratio) * 100:.1f}%" if ratio else "n/a"


def _duplicates_table(records: list[dict]) -> str:
    pairs = _duplicate_pairs(records)
    if not pairs:
        return '<table><tbody><tr><td class="muted">No within-batch similarity published.</td></tr></tbody></table>'
    rows = []
    for pair in pairs:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(pair.get('a_vendor') or ''))} ↔ {html.escape(str(pair.get('b_vendor') or ''))}</td>"
            f'<td class="num">{pair.get("description_cosine")}</td>'
            f'<td class="num">{pair.get("quantity_match")}</td>'
            f'<td class="num">{html.escape(_ratio_pct(pair.get("unit_price_ratio")))}</td>'
            f"<td>{html.escape(str(pair.get('pattern') or ''))}</td>"
            f"<td>{'FLAG' if pair.get('flagged') else 'evidence'}</td>"
            "</tr>"
        )
    return ("<table><thead><tr><th>Pair</th><th>cosine</th><th>qty match</th>"
            "<th>unit price B/A</th><th>pattern</th><th>verdict</th></tr></thead><tbody>"
            "\n".join(rows) + "</tbody></table>")


def _ai_usage_table(records: list[dict], meta: dict) -> str:
    layers: dict[str, int] = {}
    for record in records:
        layer = str(record.get("extraction_layer") or "?")
        layers[layer] = layers.get(layer, 0) + 1
    llm_verdicts = sum(1 for r in records
                       if any(v.get("verdict", {}).get("source") == "llm"
                              for v in (r.get("duplicates") or {}).get("impostor_verdicts") or []))
    llm_messages = sum(1 for r in records
                       if ((r.get("enrichment") or {}).get("message") or {}).get("source") == "llm")
    llm_rationale = sum(1 for r in records
                        if (decision_of(r).get("rationale") or {}).get("source") == "llm")
    rows = [
        ("Extraction (AI primary read)", ", ".join(f"{k}: {v}" for k, v in layers.items())),
        ("Model / prompt", f"{meta.get('model')} / {meta.get('prompt_version')}"),
        ("Similarity backend", str(meta.get("backend"))),
        ("Impostor forensic verdicts (LLM)", str(llm_verdicts)),
        ("Exception messages (LLM prose)", str(llm_messages)),
        ("Decision rationales (LLM prose)", str(llm_rationale)),
        ("Temperature", str(meta.get("temperature"))),
    ]
    return ("<table><tbody>"
            + "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
            + "</tbody></table>")


def render_report_html(records: list[dict], meta: dict, oracle_result: dict) -> str:
    records = list(records)
    totals = measured_totals(records)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if oracle_result.get("ok"):
        expected = oracle_result.get("expected_totals") or {}
        badge = (f'<span class="badge ok">ORACLE MATCH — CLEAR {expected.get("CLEAR")} · '
                 f'FLAG-REVIEW {expected.get("FLAG-REVIEW")} · HOLD {expected.get("HOLD")}</span>')
    else:
        badge = '<span class="badge warn">ORACLE DIFF — see batch_report.json</span>'
    trusted = sum(1 for r in records if r.get("total_trusted") is not None)
    subtitle = (
        f"Batch of {len(records)} invoices · CLEAR {totals.get('CLEAR', 0)} · "
        f"FLAG-REVIEW {totals.get('FLAG-REVIEW', 0)} · HOLD {totals.get('HOLD', 0)} · "
        f"verified totals {trusted}/{len(records)} · generated {meta.get('generated_at')}"
    )
    replacements = {
        "{{TITLE}}": "AP Invoice Batch — Review Report",
        "{{SUBTITLE}}": html.escape(subtitle),
        "{{ORACLE_BADGE}}": badge,
        "{{KPI_CARDS}}": _kpi_cards(records),
        "{{SUMMARY}}": html.escape(_summary_paragraph(records)),
        "{{EXCEPTION_COUNT}}": str(len(exception_records(records))),
        "{{EXCEPTIONS_TABLE}}": _exceptions_table(records),
        "{{CLEARS_TABLE}}": _clears_table(records),
        "{{DUPLICATES_TABLE}}": _duplicates_table(records),
        "{{AI_USAGE_TABLE}}": _ai_usage_table(records, meta),
        "{{FOOTER}}": ("Generated locally by the AP invoice pipeline. No invoice data leaves this "
                       "machine; the deterministic decision ladder is the only authority."),
    }
    out = template
    for token, value in replacements.items():
        out = out.replace(token, value)
    return out


def write_report_html(records: list[dict], meta: dict, oracle_result: dict,
                      path: Path | str) -> Path:
    path = Path(path)
    path.write_text(render_report_html(records, meta, oracle_result), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------- summary.md

def _summary_paragraph(records: list[dict]) -> str:
    """Plain-language lead for the brief: how many, what happened, what to do first."""
    records = list(records)
    exceptions = exception_records(records)
    holds = [r for r in exceptions if disposition_of(r) == "HOLD"]
    flags = [r for r in exceptions if disposition_of(r) == "FLAG-REVIEW"]
    trusted = sum(1 for r in records if r.get("total_trusted") is not None)
    lines = [
        f"{len(records)} invoices were read and validated before reaching an approver.",
        f"{len(exceptions)} need a human: {len(holds)} held (a mandatory condition failed) and "
        f"{len(flags)} flagged for review.",
        f"{len(records) - len(exceptions)} cleared and are ready for the approver queue.",
        f"Arithmetic verified {trusted}/{len(records)} totals; a wrapped or disputed total is "
        "never passed as trusted.",
    ]
    if exceptions:
        first = exceptions[0]
        lines.append(
            f"Start with {first.get('invoice_number')} ({first.get('vendor_name')}) — "
            f"{decision_of(first).get('next_action')}"
        )
    return " ".join(lines)


def write_summary_md(records: list[dict], meta: dict, oracle_result: dict,
                     path: Path | str) -> Path:
    path = Path(path)
    records = list(records)
    totals = measured_totals(records)
    exceptions = exception_records(records)
    holds = [r for r in exceptions if disposition_of(r) == "HOLD"]
    flags = [r for r in exceptions if disposition_of(r) == "FLAG-REVIEW"]
    clears = [r for r in records if disposition_of(r) == "CLEAR"]
    trusted = sum(1 for r in records if r.get("total_trusted") is not None)
    expected = oracle_result.get("expected_totals") or {}
    oracle_ok = bool(oracle_result.get("ok"))

    out: list[str] = []
    out.append("# AP Invoice Batch — Summary for the AP Lead\n")
    out.append(f"*Generated {meta.get('generated_at')} · model `{meta.get('model')}` · "
               f"prompt `{meta.get('prompt_version')}` · local-only "
               "(no invoice data leaves this machine)*\n")
    out.append(_summary_paragraph(records) + "\n")

    out.append("## Dispositions\n")
    out.append("| Disposition | Count | Meaning |")
    out.append("|---|---|---|")
    out.append(f"| CLEAR | {totals.get('CLEAR', 0)} | ready for the approver queue |")
    out.append(f"| FLAG-REVIEW | {totals.get('FLAG-REVIEW', 0)} | a human checks before approval |")
    out.append(f"| HOLD | {totals.get('HOLD', 0)} | a mandatory condition failed — payment blocked |")
    out.append("")
    out.append(f"**Oracle §5.3 check:** {'MATCH' if oracle_ok else 'DIFF'} — expected "
               f"CLEAR {expected.get('CLEAR', '?')} · FLAG-REVIEW {expected.get('FLAG-REVIEW', '?')} · "
               f"HOLD {expected.get('HOLD', '?')}. "
               + ("Every invoice matches the committed fixture."
                  if oracle_ok else "See `batch_report.json` for the differences."))
    out.append("")

    if exceptions:
        out.append("## What needs attention\n")
        out.append("| # | Invoice | Vendor | Disposition | Why | Next action |")
        out.append("|---|---|---|---|---|---|")
        for i, record in enumerate(exceptions, 1):
            decision = decision_of(record)
            why = "; ".join(decision.get("decision_reasons") or [])
            out.append(f"| {i} | {record.get('invoice_number')} | {record.get('vendor_name')} | "
                       f"{decision.get('disposition')} | {why} | {decision.get('next_action')} |")
        out.append("")

        for name, group in (("### Held — payment blocked\n", holds),
                            ("### Flagged for review\n", flags)):
            if not group:
                continue
            out.append(name)
            for record in group:
                decision = decision_of(record)
                out.append(f"- **{record.get('invoice_number')} · {record.get('vendor_name')}** — "
                           f"{'; '.join(decision.get('decision_reasons') or [])}")
                out.append(f"  - Next action: {decision.get('next_action')}")
            out.append("")

    out.append("## Cleared for approval\n")
    out.append(f"{len(clears)} invoices passed every rule. {trusted}/{len(records)} totals were "
               "verified by the arithmetic authority (subtotal + VAT); a wrapped or disputed total "
               "is never passed as trusted.\n")

    pairs = _duplicate_pairs(records)
    if pairs:
        out.append("## Duplicate & impostor evidence\n")
        out.append("| Pair | cosine | qty match | unit price B/A | pattern | verdict |")
        out.append("|---|---|---|---|---|---|")
        for pair in pairs:
            out.append(f"| {pair.get('a_vendor')} ↔ {pair.get('b_vendor')} | "
                       f"{pair.get('description_cosine')} | {pair.get('quantity_match')} | "
                       f"{_ratio_pct(pair.get('unit_price_ratio'))} | {pair.get('pattern')} | "
                       f"{'FLAG' if pair.get('flagged') else 'evidence'} |")
        out.append("")

    out.append("## How AI was used in this batch\n")
    layers: dict[str, int] = {}
    for record in records:
        layer = str(record.get("extraction_layer") or "?")
        layers[layer] = layers.get(layer, 0) + 1
    llm_verdicts = sum(1 for r in records
                       if any(v.get("verdict", {}).get("source") == "llm"
                              for v in (r.get("duplicates") or {}).get("impostor_verdicts") or []))
    llm_messages = sum(1 for r in records
                       if ((r.get("enrichment") or {}).get("message") or {}).get("source") == "llm")
    llm_rationale = sum(1 for r in records
                        if (decision_of(r).get("rationale") or {}).get("source") == "llm")
    out.append(f"- **Extraction** — {', '.join(f'{k}: {v}' for k, v in layers.items())}")
    out.append(f"- **Similarity backend** — `{meta.get('backend')}`")
    out.append(f"- **Impostor forensic verdicts (LLM)** — {llm_verdicts}")
    out.append(f"- **Exception messages (LLM prose)** — {llm_messages}")
    out.append(f"- **Decision rationales (LLM prose)** — {llm_rationale}")
    out.append("- Model, prompt and schema versions are pinned in `ai_telemetry.jsonl` and "
               "`audit_log.jsonl`; the disposition ladder itself is deterministic.\n")

    out.append("## Known limitations\n")
    out.append("- FX rates are a committed static table with an as-of date, not a live feed.")
    out.append("- The semantic duplicate threshold is heuristic and configurable; every flag "
               "carries its cosine/quantity/price evidence.")
    out.append("- The two 'scan' PDFs carry a text layer; a true image-only scan would need OCR.")
    out.append("- The pipeline is a filter before approvers, not an authority: it never approves "
               "or pays.")
    out.append("- A small local model misreads numbers; every misread is caught by the arithmetic "
               "authority or the second independent read, never passed silently.")
    out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------- orchestrator

def build_outputs(records: list[dict], meta: dict, out_dir: Path | str,
                  oracle_path: Path | str | None = None) -> dict:
    """Write every S11 artefact and return the paths + the oracle verdict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = list(records)
    oracle = load_oracle(oracle_path)
    oracle_result = verify_oracle(records, oracle)
    meta = dict(meta)
    meta.setdefault("generated_at", _iso_now())
    meta["oracle_check"] = oracle_result

    files = {
        "invoices_processed.csv": write_invoices_csv(records, out_dir / "invoices_processed.csv"),
        "exceptions.csv": write_exceptions_csv(records, out_dir / "exceptions.csv"),
        "summary.md": write_summary_md(records, meta, oracle_result, out_dir / "summary.md"),
        "batch_report.json": write_batch_report(records, meta, oracle_result,
                                                out_dir / "batch_report.json"),
        "audit_log.jsonl": write_audit_log(records, meta, out_dir / "audit_log.jsonl"),
        "report.html": write_report_html(records, meta, oracle_result, out_dir / "report.html"),
    }
    return {"files": {k: str(v) for k, v in files.items()}, "oracle": oracle_result}
