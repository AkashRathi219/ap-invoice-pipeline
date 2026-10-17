"""A5 — HITL telemetry and the three continuous-improvement loops (post-run / continuous).

The pipeline is a filter, not a final authority, so every AI-flavoured prediction is logged and a
human can ACCEPT / REJECT / OVERRIDE it. `feedback.py` owns that channel.

* **Telemetry** — `output/ai_telemetry.jsonl`: one immutable row per invoice per run (model,
  prompt, provenance, decision, evidence).
* **Loop A — threshold tuning** — `output/threshold_suggestions.json`: when a reviewer rejects a
  duplicate/impostor flag (a false positive), the cosine threshold for that vendor category is
  suggested upward.
* **Loop B — few-shot bank** — `config/few_shot_bank.json`: confirmed examples the prompt layer can
  quote back.
* **Loop C — SFT export** — `output/fine_tuning_dataset.jsonl`: Alpaca-style pairs for local LoRA SFT.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import decide as _decide

TELEMETRY_VERSION = "ap-telemetry-v1"
FEW_SHOT_VERSION = "ap-few-shot-v1"
FINE_TUNING_VERSION = "ap-sft-v1"
BASELINE_COSINE_THRESHOLD = 0.85
MAX_THRESHOLD_STEP = 0.05

FEEDBACK_ACTIONS = ("ACCEPT", "REJECT", "OVERRIDE")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _amount_text(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else format(value, "f")


def _decision(record: dict) -> dict:
    return record.get("decision") or _decide.decide_record(record)


def run_telemetry_row(record: dict) -> dict:
    decision = _decision(record)
    verification = record.get("verification") or {}
    vendor_match = record.get("vendor_match") or {}
    duplicates = record.get("duplicates") or {}
    verdicts = duplicates.get("impostor_verdicts") or []
    message = (record.get("enrichment") or {}).get("message") or {}
    return {
        "telemetry_version": TELEMETRY_VERSION,
        "ts": record.get("extracted_at") or _iso_now(),
        "source_file": record.get("source_file"),
        "invoice_id": record.get("invoice_number"),
        "vendor_id": vendor_match.get("vendor_id"),
        "vendor_name": record.get("vendor_name"),
        "vendor_category": (record.get("vendor_master") or {}).get("category"),
        "sha256": record.get("sha256"),
        "extraction_layer": record.get("extraction_layer"),
        "model": record.get("model"),
        "prompt_version": record.get("prompt_version"),
        "latency_ms": record.get("latency_ms"),
        "tokens": record.get("tokens"),
        "total_agrees_with_ai": verification.get("total_agrees_with_ai"),
        "line_math_ok": verification.get("line_math_ok"),
        "agreement_score": verification.get("agreement_score"),
        "review_flags": verification.get("review_flags"),
        "vendor_match_status": vendor_match.get("status"),
        "amount_eur": _amount_text((record.get("fx") or {}).get("amount_eur")),
        "fx_known": (record.get("fx") or {}).get("fx_known"),
        "disposition": decision.get("disposition"),
        "queue": decision.get("queue"),
        "risk_level": decision.get("risk_level"),
        "reason_codes": decision.get("reason_codes"),
        "variant_tells": [t.get("variant_id") for t in decision.get("variant_tells") or []],
        "within_batch_flags": (decision.get("evidence") or {}).get("duplicates", {}).get("within_batch_flagged"),
        "impostor_risk": verdicts[0]["verdict"]["risk_level"] if verdicts else None,
        "message_source": message.get("source"),
        "rationale_source": ((decision.get("rationale") or {}) or {}).get("source"),
        "reviewer_feedback": None,
    }


def write_telemetry(rows: Iterable[dict], path: Path | str) -> Path:
    path = Path(path)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def record_feedback(path: Path | str, events: Iterable[dict]) -> Path:
    path = Path(path)
    clean = []
    for event in events:
        action = str(event.get("action") or "").upper()
        if action not in FEEDBACK_ACTIONS:
            raise ValueError(f"unknown reviewer action {event.get('action')!r}; expected one of {FEEDBACK_ACTIONS}")
        clean.append({
            "ts": event.get("ts") or _iso_now(),
            "invoice_id": event.get("invoice_id"),
            "counterparty_invoice": event.get("counterparty_invoice"),
            "action": action,
            "note": event.get("note"),
            "override_disposition": event.get("override_disposition"),
        })
    with path.open("a", encoding="utf-8") as fh:
        for event in clean:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def read_feedback(path: Path | str) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def recommend_thresholds(telemetry: Iterable[dict], feedback: Iterable[dict] | None = None) -> dict:
    feedback = list(feedback or [])
    rows = {row.get("invoice_id"): row for row in telemetry}
    categories: dict[str, dict] = {}
    rejected_flags = 0
    for event in feedback:
        if event.get("action") != "REJECT":
            continue
        row = rows.get(event.get("invoice_id"))
        if not row or not row.get("impostor_risk"):
            continue
        rejected_flags += 1
        category = row.get("vendor_category") or "UNCATEGORISED"
        current = categories.get(category, {"current": BASELINE_COSINE_THRESHOLD,
                                            "suggested": BASELINE_COSINE_THRESHOLD,
                                            "rejects": 0})
        current["rejects"] += 1
        current["suggested"] = min(0.95, round(current["current"] + MAX_THRESHOLD_STEP, 3))
        categories[category] = current
    return {
        "baseline_cosine_threshold": BASELINE_COSINE_THRESHOLD,
        "rejected_flags": rejected_flags,
        "categories": categories,
        "note": ("no reviewer feedback recorded yet — baseline retained; this file is a suggestion "
                 "and never rewrites config/pipeline_config.json" if not feedback else
                 "derived from reviewer REJECT actions"),
    }


def write_threshold_suggestions(suggestions: dict, path: Path | str) -> Path:
    path = Path(path)
    payload = dict(suggestions)
    payload["generated_at"] = _iso_now()
    payload["version"] = "ap-threshold-tuning-v1"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def few_shot_exemplars(records: Iterable[dict], max_exemplars: int = 8) -> list[dict]:
    exemplars: list[dict] = []
    for record in records:
        verdicts = (record.get("duplicates") or {}).get("impostor_verdicts") or []
        if verdicts:
            verdict = verdicts[0].get("verdict") or {}
            pair = (record.get("duplicates") or {}).get("within_batch") or []
            if pair:
                exemplars.append({
                    "kind": "IMPOSTOR_CLONE",
                    "input": (f"cross-vendor pair {pair[0].get('a_vendor')} / {pair[0].get('b_vendor')} "
                              f"cosine={pair[0].get('description_cosine')} "
                              f"qty_match={pair[0].get('quantity_match')} "
                              f"unit_price_ratio={pair[0].get('unit_price_ratio')}"),
                    "output": (f"{verdict.get('risk_level')} · {verdict.get('pattern')} · "
                               f"disposition {_decision(record).get('disposition')}"),
                })
            continue
        decision = _decision(record)
        if decision.get("disposition") == "HOLD":
            exemplars.append({
                "kind": "HOLD",
                "input": f"{record.get('vendor_name')} · reasons {decision.get('reason_codes')}",
                "output": "; ".join(decision.get("decision_reasons") or []),
            })
        elif decision.get("disposition") == "FLAG-REVIEW":
            exemplars.append({
                "kind": "FLAG_REVIEW",
                "input": f"{record.get('vendor_name')} · reasons {decision.get('reason_codes')}",
                "output": "; ".join(decision.get("decision_reasons") or []),
            })
        if len(exemplars) >= max_exemplars:
            break
    return exemplars


def write_few_shot_bank(records: Iterable[dict], path: Path | str, max_exemplars: int = 8) -> Path:
    """Loop B — batch-derived exemplars. The committed seed lives in `config/few_shot_bank.json`;
    this output artefact (git-ignored) is what a fresh run derives from the measured batch."""
    path = Path(path)
    payload = {
        "version": FEW_SHOT_VERSION,
        "generated_at": _iso_now(),
        "notes": [],
        "exemplars": few_shot_exemplars(records, max_exemplars=max_exemplars),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fine_tuning_pairs(records: Iterable[dict]) -> list[dict]:
    pairs: list[dict] = []
    for record in records:
        decision = _decision(record)
        if not decision.get("review_required"):
            continue
        pairs.append({
            "format": "alpaca",
            "task": "AP_DISPOSITION_REVIEW",
            "instruction": ("Given the validated AP tells for one invoice, state the disposition, "
                            "the reasons, and the single next action."),
            "input": json.dumps({
                "invoice_number": record.get("invoice_number"),
                "vendor": record.get("vendor_name"),
                "vendor_match": (record.get("vendor_match") or {}).get("status"),
                "currency": record.get("currency"),
                "amount_eur": _amount_text((record.get("fx") or {}).get("amount_eur")),
                "po_number": record.get("po_number"),
                "reason_codes": decision.get("reason_codes"),
                "risk_level": decision.get("risk_level"),
            }, ensure_ascii=False),
            "output": json.dumps({
                "disposition": decision.get("disposition"),
                "decision_reasons": decision.get("decision_reasons"),
                "next_action": decision.get("next_action"),
            }, ensure_ascii=False),
        })
        for verdict_entry in (record.get("duplicates") or {}).get("impostor_verdicts") or []:
            verdict = verdict_entry.get("verdict") or {}
            pairs.append({
                "format": "alpaca",
                "task": "IMPOSTOR_ASSESSMENT",
                "instruction": ("Two invoices in the same batch share service descriptions. Decide "
                                "whether the counterparty invoice is a cloned charge package."),
                "input": json.dumps({
                    "invoice_number": record.get("invoice_number"),
                    "counterparty_invoice": verdict_entry.get("counterparty_invoice"),
                    "counterparty_vendor": verdict_entry.get("counterparty_vendor"),
                    "risk_level": verdict.get("risk_level"),
                    "pattern": verdict.get("pattern"),
                }, ensure_ascii=False),
                "output": json.dumps({
                    "is_impostor_risk": verdict.get("is_impostor_risk"),
                    "risk_level": verdict.get("risk_level"),
                    "pattern": verdict.get("pattern"),
                    "explanation": verdict.get("gate_explanation") or verdict.get("explanation"),
                }, ensure_ascii=False),
            })
    return pairs


def write_fine_tuning(records: Iterable[dict], path: Path | str) -> Path:
    path = Path(path)
    pairs = fine_tuning_pairs(records)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "event": "SFT_DATASET",
            "version": FINE_TUNING_VERSION,
            "generated_at": _iso_now(),
            "pair_count": len(pairs),
            "note": "Alpaca-style pairs for local LoRA SFT (llama.cpp / Unsloth) — no cloud cost.",
        }, ensure_ascii=False) + "\n")
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    return path


def _count_llm_rationale(records: list[dict]) -> int:
    from . import summarize as _summarize
    count = 0
    for record in records:
        decision = _summarize.decision_of(record)
        if ((decision.get("rationale") or {}).get("source")) == "llm":
            count += 1
    return count


def write_all(records: list[dict], out_dir: Path | str, config_dir: Path | str,
              telemetry_rows: list[dict] | None = None,
              feedback: list[dict] | None = None) -> dict:
    out_dir = Path(out_dir)
    config_dir = Path(config_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = telemetry_rows if telemetry_rows is not None else [run_telemetry_row(r) for r in records]
    suggestions = recommend_thresholds(rows, feedback or [])
    return {
        "telemetry": str(write_telemetry(rows, out_dir / "ai_telemetry.jsonl")),
        "fine_tuning": str(write_fine_tuning(records, out_dir / "fine_tuning_dataset.jsonl")),
        "few_shot_bank": str(write_few_shot_bank(records, out_dir / "few_shot_bank.json")),
        "threshold_suggestions": str(write_threshold_suggestions(
            suggestions, out_dir / "threshold_suggestions.json")),
    }
