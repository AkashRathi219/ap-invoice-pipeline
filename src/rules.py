"""S6 — The six disposition rules R1..R6: one function each, deterministic, fail-safe.

R1  vendor status        — unknown -> HOLD · INACTIVE -> HOLD · fuzzy confirmed by L3 country corroboration
R2  currency             — invoice currency vs master approved_currency (L4) -> mismatch -> FLAG-REVIEW
R3  spend threshold     — EUR-normalised amount (L5, via `src/fx.py`) vs master threshold -> above -> FLAG-REVIEW
R4  dual approval       — master requires_dual_approval (L6) -> TRUE -> FLAG-REVIEW (two approvers)
R6  PO presence         — P6 `po_number` present? -> missing -> HOLD
R5a/R5b duplicate tells are produced by `src/duplicates.py` and appended to this list.

Status vocabulary: `pass` · `skip` (not applicable, e.g. no master record to compare against) ·
`flag` (needs a human: mismatch/above-threshold/dual-approval/unverified-total/duplicate evidence) ·
`HOLD` (structural stop: unknown or inactive vendor, unrecognised currency, missing PO).

Disposition ladder: HOLD > FLAG-REVIEW > CLEAR. The LLM never writes a disposition —
these reason codes are the only source of truth (the message layer reads them, never overrides them).
"""
from __future__ import annotations

from .vendor_match import VendorMatch, VendorRow

CLEAR = "CLEAR"
FLAG_REVIEW = "FLAG-REVIEW"
HOLD = "HOLD"

_KNOWN_CURRENCIES = {"EUR", "GBP", "USD", "CHF"}


def check_vendor(vendor_match: VendorMatch) -> dict:
    """Evaluates Rule R1: Vendor master existence and operational status.

    - Unknown vendor -> HOLD ('R1_unknown_vendor')
    - Inactive vendor -> HOLD ('R1_inactive_vendor')
    - Fuzzy match with disagreeing country -> flag ('R1_country_conflict')

    Where called:
        Called inside run_rules().
    """
    if vendor_match.status == "unknown":
        return {"rule_id": "R1", "status": HOLD, "reason_code": "R1_unknown_vendor",
                "detail": f"vendor '{vendor_match.vendor_name}' not in master"}
    if vendor_match.status == "fuzzy":
        if vendor_match.country_match is False:
            return {"rule_id": "R1", "status": "flag", "reason_code": "R1_country_conflict",
                "detail": f"fuzzy vendor {vendor_match.vendor_name_matched} but country disagrees"}
        return {"rule_id": "R1", "status": "pass", "reason_code": None,
                "detail": f"fuzzy vendor confirmed {vendor_match.vendor_name_matched}"}
    row = vendor_match.vendor_row
    if row is not None and row.status == "INACTIVE":
        return {"rule_id": "R1", "status": HOLD, "reason_code": "R1_inactive_vendor",
                "detail": f"vendor {row.vendor_id} status {row.status}"}
    return {"rule_id": "R1", "status": "pass", "reason_code": None,
            "detail": f"vendor {vendor_match.vendor_id} ok"}


def check_currency(record: dict, vendor_match: VendorMatch) -> dict:
    """Evaluates Rule R2: Compares invoice billing currency against master approved currency.

    - Unrecognised currency ISO code -> HOLD ('R2_currency_unrecognised')
    - Currency differs from vendor master approved currency -> flag ('R2_currency_mismatch')

    Where called:
        Called inside run_rules().
    """
    currency = str(record.get("currency") or "").strip().upper()
    row = vendor_match.vendor_row
    if row is None:
        return {"rule_id": "R2", "status": "skip", "reason_code": None,
                "detail": "no master record — approved currency not applicable (R1 already holds)"}
    approved = str(getattr(row, "approved_currency", None) or "").strip().upper()
    if not currency or currency not in _KNOWN_CURRENCIES:
        return {"rule_id": "R2", "status": HOLD, "reason_code": "R2_currency_unrecognised",
                "detail": f"currency {currency!r} not an ISO code we clear"}
    if not approved:
        return {"rule_id": "R2", "status": HOLD, "reason_code": "R2_approved_currency_missing",
                "detail": f"vendor {vendor_match.vendor_id} has no approved currency"}
    if currency != approved:
        return {"rule_id": "R2", "status": "flag", "reason_code": "R2_currency_mismatch",
                "detail": f"invoice {currency} != approved {approved} for {vendor_match.vendor_id}"}
    return {"rule_id": "R2", "status": "pass", "reason_code": None,
            "detail": f"{currency} approved for {vendor_match.vendor_id}"}


def check_threshold(amount_eur, vendor_row: VendorRow | None) -> dict:
    """Evaluates Rule R3: Checks if EUR-converted spend exceeds vendor spend threshold.

    - Disagreeing or unverified total -> flag ('R3_amount_unavailable')
    - Amount > spend_threshold_eur -> flag ('R3_above_threshold')

    Where called:
        Called inside run_rules().
    """
    if amount_eur is None:
        return {"rule_id": "R3", "status": "flag", "reason_code": "R3_amount_unavailable",
                "detail": "readers disagree on the total — no verified EUR amount to compare "
                          "against the threshold (approver reviews the read)"}
    threshold = vendor_row.spend_threshold_eur if vendor_row is not None else None
    if threshold is None:
        return {"rule_id": "R3", "status": HOLD, "reason_code": "R3_threshold_unavailable",
                "detail": f"vendor {vendor_row.vendor_id if vendor_row else '?'} has no threshold"}
    if amount_eur > threshold:
        return {"rule_id": "R3", "status": "flag", "reason_code": "R3_above_threshold",
                "detail": f"{amount_eur} > {threshold} EUR (after FX to EUR)"}
    return {"rule_id": "R3", "status": "pass", "reason_code": None,
            "detail": f"{amount_eur} <= {threshold} EUR"}


def check_dual_approval(vendor_row: VendorRow | None) -> dict:
    """Evaluates Rule R4: Checks if vendor configuration mandates dual approvers.

    - requires_dual_approval == True -> flag ('R4_dual_approval_required')

    Where called:
        Called inside run_rules().
    """
    if vendor_row is not None and vendor_row.requires_dual_approval:
        return {"rule_id": "R4", "status": "flag", "reason_code": "R4_dual_approval_required",
                "detail": f"dual approval required for {vendor_row.vendor_id}"}
    return {"rule_id": "R4", "status": "pass", "reason_code": None,
            "detail": "no dual approval required"}


def check_po(record: dict) -> dict:
    """Evaluates Rule R6: Mandatory PO reference presence check.

    - Missing or blank po_number -> HOLD ('R6_po_missing')

    Where called:
        Called inside run_rules().
    """
    po_number = record.get("po_number")
    if po_number is None or not str(po_number).strip():
        return {"rule_id": "R6", "status": HOLD, "reason_code": "R6_po_missing",
                "detail": f"no PO reference on {record.get('invoice_number') or record.get('source_file')}"}
    return {"rule_id": "R6", "status": "pass", "reason_code": None,
            "detail": f"PO {po_number}"}


def run_rules(record: dict, vendor_match: VendorMatch, amount_eur) -> list[dict]:
    """Applies core deterministic AP rules (R1, R2, R3, R4, R6) against an invoice record.

    Args:
        record: Extracted and verified invoice record.
        vendor_match: Matched master vendor result.
        amount_eur: EUR amount from fx.py to_eur().

    Returns:
        List of rule evaluation result dictionaries.

    Where called:
        Called by run.py in Pass 1, and by tests/test_rules.py.

    Where it sends information:
        Saved into record['rules']['results']. Combined later with R5b (historic)
        and R5a (batch duplicates) to compute final disposition.
    """
    return [
        check_vendor(vendor_match),
        check_currency(record, vendor_match),
        check_threshold(amount_eur, vendor_match.vendor_row),
        check_dual_approval(vendor_match.vendor_row),
        check_po(record),
    ]


def disposition(results: list[dict]) -> str:
    """Derives aggregate status according to the strict priority ladder: HOLD > FLAG-REVIEW > CLEAR.

    Args:
        results: List of rule result dictionaries.

    Returns:
        One of 'HOLD', 'FLAG-REVIEW', or 'CLEAR'.

    Where called:
        Called in run.py during Pass 1, again after Pass 2 batch duplicate insertion,
        and inside decide.py.
    """
    if any(r["status"] == HOLD for r in results):
        return HOLD
    if any(r["status"] == "flag" for r in results):
        return FLAG_REVIEW
    return CLEAR


def active_reasons(results: list[dict]) -> list[dict]:
    """Filters rule results to only those that triggered a non-CLEAR condition."""
    return [r for r in results if r.get("reason_code")]


def reason_codes(results: list[dict]) -> list[str]:
    """Extracts ordered list of reason code strings (e.g. ['R1_inactive_vendor', 'R6_po_missing'])."""
    return [r["reason_code"] for r in active_reasons(results)]
