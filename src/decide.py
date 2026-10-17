"""S10 — Decide: every tell folded into one decision object per invoice.

This module is the single owner of the decide chain. It does **not** re-run the rules; it reads
the rule results produced upstream (R1/R2/R3/R4/R6 by `rules.py`, R5a/R5b by `duplicates.py`) and
turns them into one structured, reviewer-facing decision:

* **disposition** — `CLEAR` / `FLAG-REVIEW` / `HOLD`, computed by `rules.disposition` (one ladder,
  one source of truth; HOLD > FLAG-REVIEW > CLEAR).
* **queue** — where the invoice goes next (approver queue / review queue / held).
* **reason_codes + decision_reasons** — the machine reason codes and the plain-language "why".
* **variant_tells** — one F10 tag per error observed (V1–V6), riding in every response.
* **evidence** — the scored proof (match ratio, cosine, FX rate, arithmetic status) rolled up.
* **rationale** — A4: an optional one-paragraph reviewer summary. The LLM may *describe* the
  decision, never change it (read-only, post-decision); the deterministic reason strings stand
  regardless.

The ladder is deliberately rules-only: verification noise (e.g. a line the model mis-transcribed)
remains evidence and does not by itself stop an invoice whose arithmetic authority is intact.
"""
from __future__ import annotations

from . import rules as _rules
from .enrich import ACTION_BY_REASON, DEFAULT_ACTION
from .llm_provider import LLMBadOutput, LLMUnavailable, chat_json

CLEAR = _rules.CLEAR
FLAG_REVIEW = _rules.FLAG_REVIEW
HOLD = _rules.HOLD

QUEUE_BY_DISPOSITION = {
    CLEAR: "APPROVER_QUEUE",
    FLAG_REVIEW: "REVIEW_QUEUE",
    HOLD: "HELD",
}
OUTCOME_BY_DISPOSITION = {
    CLEAR: "approved_for_approver_queue",
    FLAG_REVIEW: "routed_to_review_queue",
    HOLD: "held_pending_completion",
}

RISK_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

# ---- F10 variant taxonomy (PIPELINE_PLAN.md §3): one tag per error observed ----------------
V1 = {"variant_id": "V1", "variant_class": "FORMAT_VARIANT", "ai_highlight": "Format class detected"}
V2 = {"variant_id": "V2", "variant_class": "VENDOR_VARIANT", "ai_highlight": "Match class + score"}
V3 = {"variant_id": "V3", "variant_class": "ARITHMETIC_VARIANT", "ai_highlight": "Arithmetic authority"}
V4 = {"variant_id": "V4", "variant_class": "FX_VARIANT", "ai_highlight": "FX tells"}
V5 = {"variant_id": "V5", "variant_class": "DUPLICATE_VARIANT", "ai_highlight": "Scored evidence"}
V6 = {"variant_id": "V6", "variant_class": "DECISION_VARIANT", "ai_highlight": "Decision tells"}

GENERIC_REASONS = {
    CLEAR: "all rules passed — released to the approver queue",
    FLAG_REVIEW: "at least one rule flagged the invoice for review before approval",
    HOLD: "a mandatory condition failed — payment is structurally blocked",
}


# ------------------------------------------------------------------------------ variant tells

def _variant(template: dict, *, error_observed: str, severity: str, tolerance) -> dict:
    tell = dict(template)
    tell.update({"error_observed": error_observed, "severity": severity, "tolerance": tolerance})
    return tell


def _disposition_of(record: dict) -> str:
    """Computes final disposition from rule results, ensuring idempotency and robustness."""
    disposition = (record.get("rules") or {}).get("disposition")
    if disposition in (CLEAR, FLAG_REVIEW, HOLD):
        return disposition
    return _rules.disposition((record.get("rules") or {}).get("results") or [])


def variant_tells(record: dict) -> list[dict]:
    """Generates standardized F10 variant tags (V1–V6) for any anomalies observed on an invoice.

    Where called:
        Called inside decide_record() to enrich the decision object.
    """
    tells: list[dict] = []
    verification = record.get("verification") or {}
    vendor_match = record.get("vendor_match") or {}
    disposition = _disposition_of(record)

    # V1 — a non-standard layout: the scan label set (format class B) or the offline regex path.
    if record.get("format_tag") == "B":
        tells.append(_variant(V1, error_observed="scanned / variant label layout (format class B)",
                              severity="high", tolerance=0))
    elif str(record.get("extraction_layer") or "") == "regex_fallback":
        tells.append(_variant(V1, error_observed="AI unreachable — named regex fallback path",
                              severity="high", tolerance=0))

    # V2 — vendor-name fuzz: exact match failed, the normalised match carried it.
    if vendor_match.get("status") == "fuzzy":
        tells.append(_variant(V2, error_observed="vendor-name fuzz — exact match failed",
                              severity="high", tolerance=0.85))

    # V3 — the arithmetic trap: a wrapped or disputed total in the text layer.
    if verification.get("wrap_detected"):
        tells.append(_variant(V3, error_observed="line-wrapped total in the text layer",
                              severity="critical", tolerance=0))
    elif verification.get("total_status") == "mismatch":
        tells.append(_variant(V3, error_observed="document total disagrees with arithmetic authority",
                              severity="critical", tolerance=0))

    # V4 — foreign currency normalised to EUR.
    currency = str(record.get("currency") or "").upper()
    if currency and currency != "EUR" and (record.get("fx") or {}).get("fx_known"):
        tells.append(_variant(V4, error_observed=f"{currency} invoice — FX to EUR",
                              severity="high", tolerance=0))

    # V5 — duplicate evidence: impostor pair, historic near-duplicate, or a shared package.
    within = ((record.get("duplicates") or {}).get("within_batch")) or []
    verdicts = ((record.get("duplicates") or {}).get("impostor_verdicts")) or []
    historic = (record.get("duplicates") or {}).get("vs_historic") or {}
    if verdicts:
        tells.append(_variant(V5, error_observed="cross-vendor impostor pair (clone-package evidence)",
                              severity="critical", tolerance="tolerance AND date-window"))
    elif any(p.get("flagged") for p in within):
        tells.append(_variant(V5, error_observed="flagged within-batch clone package",
                              severity="high", tolerance="tolerance AND date-window"))
    elif historic.get("reason_code"):
        tells.append(_variant(V5, error_observed="near-duplicate of a historic invoice",
                              severity="medium", tolerance="tolerance AND date-window"))
    elif within:
        tells.append(_variant(V5, error_observed="shared descriptions within the batch",
                              severity="low", tolerance="scored evidence"))

    # V6 — the disposition itself is an observable variant per the golden-file fixture.
    if disposition != CLEAR:
        tells.append(_variant(V6, error_observed=f"disposition class {disposition}",
                              severity="high", tolerance=0))
    return tells


# --------------------------------------------------------------------------- decision reasons

def _risk_level(record: dict) -> str | None:
    verdicts = ((record.get("duplicates") or {}).get("impostor_verdicts")) or []
    if verdicts:
        return verdicts[0].get("verdict", {}).get("risk_level") or "HIGH"
    disposition = _disposition_of(record)
    if disposition == HOLD:
        return "HIGH"
    if disposition == FLAG_REVIEW:
        return "MEDIUM"
    return None


def why_text(result: dict) -> str:
    """Formats human-readable explanation string for an individual rule blocker."""
    code = result.get("reason_code")
    detail = str(result.get("detail") or "").strip()
    if code and detail:
        return f"{result.get('rule_id')}: {detail}"
    return detail or str(result.get("rule_id") or "rule result")


def decision_reasons(record: dict, disposition: str | None = None) -> list[str]:
    """Generates list of deterministic plain-language reason strings explaining the disposition."""
    disposition = disposition or _disposition_of(record)
    results = (record.get("rules") or {}).get("results") or []
    active = [r for r in results if r.get("status") in ("flag", HOLD)]
    if not active:
        return [GENERIC_REASONS.get(disposition, GENERIC_REASONS[CLEAR])]
    return [why_text(r) for r in active]


def next_action_for(reason_codes: list[str]) -> str | None:
    """Selects highest priority remediation action code for non-CLEAR invoices."""
    for code in reason_codes:
        if code in ACTION_BY_REASON:
            return ACTION_BY_REASON[code]
    return DEFAULT_ACTION if reason_codes else None


# --------------------------------------------------------------------------------- evidence

def evidence_summary(record: dict) -> dict:
    """Collates all numeric, cryptographic, and categorical evidence backing the decision."""
    vendor_match = record.get("vendor_match") or {}
    verification = record.get("verification") or {}
    duplicate_block = record.get("duplicates") or {}
    verdicts = duplicate_block.get("impostor_verdicts") or []
    historic = duplicate_block.get("vs_historic") or {}

    return {
        "vendor_match": {
            "status": vendor_match.get("status"),
            "vendor_id": vendor_match.get("vendor_id"),
            "fuzzy_score": vendor_match.get("fuzzy_score"),
            "country_match": vendor_match.get("country_match"),
        },
        "fx": {
            "currency": (record.get("fx") or {}).get("currency"),
            "amount_eur": (record.get("fx") or {}).get("amount_eur"),
            "rate_to_eur": (record.get("fx") or {}).get("rate_to_eur"),
            "as_of": (record.get("fx") or {}).get("as_of"),
        },
        "threshold_eur": (record.get("vendor_master") or {}).get("spend_threshold_eur"),
        "arithmetic": {
            "total_status": verification.get("total_status"),
            "wrap_detected": verification.get("wrap_detected"),
            "trusted_total": record.get("total_trusted"),
            "agreement_score": verification.get("agreement_score"),
        },
        "duplicates": {
            "historic_status": historic.get("status"),
            "historic_reason": historic.get("reason_code"),
            "within_batch_published": len(duplicate_block.get("within_batch") or []),
            "within_batch_flagged": sum(
                1 for p in (duplicate_block.get("within_batch") or []) if p.get("flagged")
            ),
            "impostor_risk": (_risk_level(record) if verdicts else None),
        },
        "vendor_master_status": (record.get("vendor_master") or {}).get("status"),
    }


# --------------------------------------------------------------------------------- decide

def decide_record(record: dict, llm_cfg: dict | None = None) -> dict:
    """Synthesises all upstream signals into one canonical decision object.

    Assigns disposition (CLEAR, FLAG-REVIEW, HOLD), destination queue (APPROVER_QUEUE,
    REVIEW_QUEUE, HELD), blockers, next action, and reviewer rationale.

    Where called:
        Called inside decide_all().

    Where it sends information:
        Saved onto the record under the 'decision' key.
    """
    results = (record.get("rules") or {}).get("results") or []
    disposition = _rules.disposition(results)
    reason_codes = _rules.reason_codes(results)
    decision = {
        "disposition": disposition,
        "queue": QUEUE_BY_DISPOSITION[disposition],
        "outcome": OUTCOME_BY_DISPOSITION[disposition],
        "review_required": disposition != CLEAR,
        "reason_codes": reason_codes,
        "decision_reasons": decision_reasons(record, disposition),
        "blockers": [
            {"rule_id": r.get("rule_id"), "status": r.get("status"),
             "reason_code": r.get("reason_code"), "detail": r.get("detail")}
            for r in results if r.get("status") in ("flag", HOLD)
        ],
        "evidence": evidence_summary(record),
        "risk_level": _risk_level(record),
        "variant_tells": variant_tells(record),
        "next_action": next_action_for(reason_codes),
        "rationale": None,
    }
    if disposition != CLEAR:
        decision["rationale"] = reviewer_rationale(record, decision, llm_cfg)
    return decision


def decide_all(records: list[dict], llm_cfg: dict | None = None) -> list[dict]:
    """Iterates across all records in the batch, attaching a decision object to each.

    Where called:
        Called by run.py at Step S10 in Pass 2.

    Where it sends information:
        Populates record['decision'] across the batch, consumed by summarize.py to build reports.
    """
    for record in records:
        record["decision"] = decide_record(record, llm_cfg)
    return records


# ---------------------------------------------------------------------- A4 rationale (read-only)

RATIONALE_PROMPT_VERSION = "ap-decide-v1"

RATIONALE_SYSTEM = (
    "You are an accounts-payable analyst explaining one decision to a reviewer. Summarise WHY the "
    "invoice is not cleared, using only the supplied reason codes and evidence. Never change the "
    "disposition, never invent amounts, vendors, dates or reasons. Answer only with the JSON object."
)

RATIONALE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _rationale_prompt(record: dict) -> str:
    disposition = _disposition_of(record)
    results = (record.get("rules") or {}).get("results") or []
    codes = [r.get("reason_code") for r in results if r.get("reason_code")]
    reasons = "\n".join(f"- {r}" for r in decision_reasons(record, disposition))
    evidence = evidence_summary(record)
    return (
        f"Invoice {record.get('invoice_number') or record.get('source_file')} from "
        f"{record.get('vendor_name')} for {record.get('total_trusted') or 'an unverified total'} "
        f"{record.get('currency')}. The deterministic disposition is {disposition}.\n\n"
        f"Authoritative reason codes: {codes}\n\n"
        f"Authoritative reasons:\n{reasons}\n\n"
        f"Evidence: vendor match {evidence['vendor_match']['status']}, FX rate "
        f"{evidence['fx']['rate_to_eur']}, total status {evidence['arithmetic']['total_status']}, "
        f"impostor risk {evidence['duplicates']['impostor_risk']}.\n\n"
        "Write one short paragraph (2-4 sentences) for the AP reviewer. Copy every identifier "
        "exactly; do not add or remove a reason. Return JSON: summary."
    )


def template_rationale(record: dict) -> str:
    """Generates deterministic baseline rationale text when local LLM is offline or disabled."""
    disposition = _disposition_of(record)
    reasons = decision_reasons(record, disposition)
    action = next_action_for(_rules.reason_codes((record.get("rules") or {}).get("results") or []))
    parts = [
        f"{record.get('invoice_number') or record.get('source_file')} is {disposition}:",
        " ".join(reasons),
    ]
    if action:
        parts.append(f"Next action: {action}")
    return " ".join(parts)


def reviewer_rationale(record: dict, decision: dict, llm_cfg: dict | None = None) -> dict:
    """Generates concise explanatory narrative for human reviewer via LLM or template fallback.

    Note:
        Strictly read-only and executed post-decision: the LLM cannot alter the disposition.

    Where called:
        Called inside decide_record() for non-CLEAR invoices.

    Where it sends information:
        Saved into record['decision']['rationale'], visible in summary.md and report.html.
    """
    fallback = {
        "source": "template",
        "text": template_rationale(record),
        "model": None,
        "prompt_version": None,
        "latency_ms": None,
    }
    if not llm_cfg:
        return fallback
    try:
        result = chat_json(
            RATIONALE_SYSTEM,
            _rationale_prompt(record),
            RATIONALE_SCHEMA,
            model=llm_cfg["model"],
            prompt_version=RATIONALE_PROMPT_VERSION,
            temperature=llm_cfg.get("temperature", 0.0),
            base_url=llm_cfg["base_url"],
        )
    except (LLMUnavailable, LLMBadOutput):
        return fallback
    text = str(result.data.get("summary") or "").strip()
    if not text:
        return fallback
    return {
        "source": "llm",
        "text": text[:800],
        "model": result.model,
        "prompt_version": result.prompt_version,
        "latency_ms": result.latency_ms,
    }
