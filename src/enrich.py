"""S9 — Enrichment: free-text note tells + plain-language messages for the review queue.

Two jobs, both additive — enrichment never changes a disposition (it can only help a human):

* **note tells (P14).** A vendor note is classified into an informational tell. The note is
  surfaced with its verbatim text and never becomes a blocker on its own (F9): a re-submission
  statement is a reason to *check*, not a reason to hold.
* **message authoring (S9 → S10).** For every non-CLEAR invoice the pipeline drafts the note a
  human will read: what is wrong, why payment is blocked, and the single next action. The reason
  codes computed by the rule layer are authoritative and are passed to the model verbatim; the
  model is instructed not to invent amounts, dates, or reasons. If the local model is unavailable
  the same message is produced from a deterministic template (`source` names which path ran).
"""
from __future__ import annotations

import re

from .llm_provider import LLMBadOutput, LLMUnavailable, chat_json

MESSAGE_PROMPT_VERSION = "ap-enrich-v1"

NOTE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("VENDOR_RESUBMISSION", re.compile(r"re-?\s*submission|resubmit", re.IGNORECASE)),
    ("DELIVERY_DOCUMENTATION", re.compile(r"delivery\s+document", re.IGNORECASE)),
    ("CREDIT_NOTE_REFERENCE", re.compile(r"credit\s+note", re.IGNORECASE)),
    ("CORRECTION_NOTICE", re.compile(r"\bcorrect(?:ed|ion)\b", re.IGNORECASE)),
)

# Reason -> the one action that unblocks the invoice (also the LLM's instruction).
ACTION_BY_REASON = {
    "R1_unknown_vendor": "Add the vendor to the master (or reject the invoice) before any payment.",
    "R1_inactive_vendor": "Confirm the vendor is reactivated in the master before payment.",
    "R2_currency_unrecognised": "Confirm the currency shown on the invoice before any conversion.",
    "R2_approved_currency_missing": "Agree the invoicing currency with the vendor and record it in the master.",
    "R2_currency_mismatch": "Ask the vendor to re-issue in the approved currency or record an approved exception.",
    "R3_above_threshold": "Route the invoice for the additional approval required above the vendor threshold.",
    "R3_amount_unavailable": "Re-read the total (the two readers disagreed) or obtain the vendor's PDF confirmation.",
    "R3_threshold_unavailable": "Record a spend threshold for the vendor in the master.",
    "R4_dual_approval_required": "Collect the second approver's sign-off before payment.",
    "R5a_clone_package": "Verify with the requesting department that this is not a duplicate charge before paying.",
    "R5b_exact_duplicate": "Check the historic invoice — payment may already have been made.",
    "R5b_near_duplicate": "Confirm this is a new delivery and not a re-issued historic invoice.",
    "R6_po_missing": "Obtain the PO reference from the requester and attach it to the invoice.",
}
DEFAULT_ACTION = "Review the invoice manually before release."

# Reasons a vendor can fix directly; everything else stays an internal review note.
VENDOR_ACTIONABLE = {"R2_currency_mismatch", "R2_approved_currency_missing", "R6_po_missing"}

MESSAGE_SYSTEM = (
    "You are an accounts-payable analyst writing one short note about a single invoice: either an "
    "internal note for the reviewer or a short email to the vendor. Use only the blockers supplied — "
    "never invent amounts, dates, vendors or reasons. Be concrete and calm. Answer only with the JSON "
    "object required by the schema."
)

MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "next_action": {"type": "string"},
    },
    "required": ["subject", "body", "next_action"],
}


def note_tells(record: dict) -> list[dict]:
    """Classifies free-text notes into informational tells (e.g. VENDOR_RESUBMISSION). Never blocks.

    Where called:
        Called inside enrich_record().
    """
    note = str(record.get("free_text_note") or "").strip()
    if not note:
        return []
    tells = [
        {"code": code, "impact": "informational", "text": note}
        for code, pattern in NOTE_PATTERNS
        if pattern.search(note)
    ]
    if not tells:
        tells.append({"code": "FREE_TEXT_PRESENT", "impact": "informational", "text": note})
    return tells


def blockers_of(record: dict) -> list[dict]:
    """Filters rule results to only those with 'flag' or 'HOLD' status."""
    results = (record.get("rules") or {}).get("results") or []
    return [r for r in results if r.get("status") in ("flag", "HOLD")]


def reason_codes_of(record: dict) -> list[str]:
    """Returns list of active reason codes from non-CLEAR rule results."""
    return [r.get("reason_code") for r in blockers_of(record) if r.get("reason_code")]


def next_action_for(reason_codes: list[str]) -> str:
    """Looks up deterministic remediation instruction for the highest priority blocker code."""
    for code in reason_codes:
        if code in ACTION_BY_REASON:
            return ACTION_BY_REASON[code]
    return DEFAULT_ACTION


def amount_text(record: dict) -> str:
    """Formats monetary amount string with currency code or notes unverified status."""
    total = record.get("total_trusted")
    currency = record.get("currency") or "EUR"
    if total is None:
        return f"an unverified total ({currency})"
    return f"{total if isinstance(total, str) else format(total, 'f')} {currency}"


def duplicate_note(record: dict) -> str:
    """Summarises duplicate match findings into a concise string for reviewers."""
    parts: list[str] = []
    for r in (record.get("rules") or {}).get("results") or []:
        if r.get("rule_id") == "R5a" and r.get("status") == "flag":
            parts.append(f"clone-package match with invoice {r.get('counterparty')}")
    historic = (record.get("duplicates") or {}).get("vs_historic") or {}
    if historic.get("reason_code"):
        parts.append(f"historic evidence: {historic.get('detail')}")
    return "; ".join(parts)


def _context_line(record: dict) -> str:
    """Constructs header line with vendor ID, status, terms, and date."""
    master = record.get("vendor_master") or {}
    vendor_id = (record.get("vendor_match") or {}).get("vendor_id") or "no master record"
    bits = [str(record.get("vendor_name") or "?"), f"({vendor_id}"]
    if master.get("status"):
        bits.append(f", {master['status']}")
    bits.append(")")
    if master.get("payment_terms_days"):
        bits.append(f"[terms {master['payment_terms_days']}d]")
    return (
        f"{''.join(bits)} invoice {record.get('invoice_number') or record.get('source_file')} "
        f"dated {record.get('invoice_date')} for {amount_text(record)}"
    )


def compose_message(record: dict) -> dict:
    """Builds deterministic fallback reviewer note when local LLM is offline or disabled.

    Where called:
        Called by author_message() as base structure and fallback.
    """
    blockers = blockers_of(record)
    codes = [b["reason_code"] for b in blockers if b.get("reason_code")]
    action = next_action_for(codes)
    channel = "vendor" if any(c in VENDOR_ACTIONABLE for c in codes) else "internal"
    master = record.get("vendor_master") or {}
    to = (master.get("contact_email") if channel == "vendor" else None) or "ap-review-queue"

    reasons = "\n".join(
        f"- {b.get('rule_id')} {b.get('reason_code')}: {b.get('detail')}"
        for b in blockers if b.get("reason_code")
    )
    extra = duplicate_note(record)

    return {
        "channel": channel,
        "to": to,
        "subject": (f"[{(record.get('rules') or {}).get('disposition', 'REVIEW')}] "
                    f"{record.get('invoice_number') or record.get('source_file')} — "
                    f"{record.get('vendor_name')} — {amount_text(record)}"),
        "body": (f"{_context_line(record)} cannot be released automatically.\n"
                 f"{reasons}\n"
                 + (f"Duplicate evidence: {extra}\n" if extra else "")
                 + f"Next action: {action}"),
        "next_action": action,
        "reason_codes": codes,
        "source": "template",
        "model": None,
        "prompt_version": None,
        "latency_ms": None,
    }


def _message_prompt(record: dict, blockers: list[dict], channel: str, extra: str) -> str:
    """Prepares user prompt containing invoice context and blockers for LLM authoring."""
    reasons = "\n".join(
        f"- {b.get('rule_id')} {b.get('reason_code')}: {b.get('detail')}"
        for b in blockers if b.get("reason_code")
    )
    audience = ("the vendor's accounts-payable contact"
                if channel == "vendor" else "the internal AP reviewer")
    return (
        f"Write one short note for {audience} about this invoice.\n\n"
        f"Invoice context: {_context_line(record)}\n"
        f"Written by: {record.get('vendor_name')}\n"
        f"Blockers (authoritative — do not add or drop any):\n{reasons}\n"
        + (f"Duplicate evidence: {extra}\n" if extra else "")
        + "\nReturn JSON with: subject (one line), body (2-4 sentences: what is wrong and why payment "
          "is blocked), next_action (one sentence, the single thing to do next). "
          "Copy every identifier (invoice numbers, amounts, vendor names, reason codes) exactly as "
          "given above — never invent or alter one."
    )


def author_message(record: dict, llm_cfg: dict | None = None) -> dict:
    """Generates plain-language exception message via LLM or deterministic template.

    Note:
        The LLM drafts the explanatory prose (subject/body). The next_action field
        remains strictly deterministic from ACTION_BY_REASON.

    Where called:
        Called inside enrich_record() for non-CLEAR invoices.

    Where it sends information:
        Saved into record['enrichment']['message'] and exported into exceptions.csv.
    """
    fallback = compose_message(record)
    if not llm_cfg:
        return fallback
    blockers = blockers_of(record)
    try:
        result = chat_json(
            MESSAGE_SYSTEM,
            _message_prompt(record, blockers, fallback["channel"], duplicate_note(record)),
            MESSAGE_SCHEMA,
            model=llm_cfg["model"],
            prompt_version=MESSAGE_PROMPT_VERSION,
            temperature=llm_cfg.get("temperature", 0.0),
            base_url=llm_cfg["base_url"],
        )
    except (LLMUnavailable, LLMBadOutput):
        return fallback

    data = result.data
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        return fallback
    message = dict(fallback)
    message.update({
        "subject": subject[:200],
        "body": body[:800],
        "source": "llm",
        "model": result.model,
        "prompt_version": result.prompt_version,
        "latency_ms": result.latency_ms,
        "ai_next_action": str(data.get("next_action") or "")[:300] or None,
    })
    return message


def enrich_record(record: dict, llm_cfg: dict | None = None, author: bool = True) -> dict:
    """Enriches an invoice record with free-text note classifications and exception notes.

    Where called:
        Called by run.py in Pass 2 at Step S9.

    Where it sends information:
        Attached as record['enrichment']. Feeds exceptions.csv and report.html.
    """
    disposition = (record.get("rules") or {}).get("disposition") or "CLEAR"
    message = author_message(record, llm_cfg) if (author and disposition != "CLEAR") else None
    return {"note_tells": note_tells(record), "message": message}
