"""S4 — Verification: arithmetic authority + AI<->regex dual-read agreement (G7: AI proposes, math disposes).

Policies (DEVELOPMENT_PLAN.md §3):
 * Two INDEPENDENT reads witness every invoice: the AI (primary) and the regex stream.
 * TRUSTED TOTAL = `subtotal + vat_amount` — but only when BOTH reads agree on the inputs.
   If they disagree (e.g. the AI misreads a number), the arithmetic is built on sand: the
   record is flagged for review and NO trusted total is published. Self-consistent garbage
   (every number misread the same way) is exactly what this rule catches.
 * The document's own TOTAL line is reconciled against the authority: `clean` when the naive
   read matches, `wrap_resolved` when the page-wrapped tokens join to it, `mismatch` otherwise.
 * Fields arithmetic cannot arbitrate (name, dates, PO...): AI value stands as primary and any
   disagreement is a review flag for the decide layer — evidence, never silently ignored.
 * Both-null counts as agreement (both reads see the field absent — SparkIT's PO).
"""
from __future__ import annotations

from decimal import Decimal

from .extract_regex import parse_money

MONEY_TOLERANCE = Decimal("0.01")

_COMPARED_FIELDS = (
    "vendor_name", "vendor_vat", "invoice_number", "invoice_date",
    "po_number", "currency", "subtotal", "vat_amount", "total",
)


def _as_dec(value) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        return parse_money(value)
    return None


def _money_close(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= MONEY_TOLERANCE


def _field_agrees(field: str, ai_value, corrob_value) -> bool:
    if field in ("subtotal", "vat_amount", "total"):
        return _money_close(_as_dec(ai_value), _as_dec(corrob_value))
    a = ai_value if not isinstance(ai_value, Decimal) else ai_value
    b = corrob_value if not isinstance(corrob_value, Decimal) else corrob_value
    if a is None and b is None:
        return True  # both reads agree the field is absent
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def verify(record: dict, corroboration: dict | None = None) -> dict:
    """Adds a `verification` block to the primary record; returns the same record."""
    v: dict = {"ok": True, "failures": [], "review_flags": []}

    subtotal = _as_dec(record.get("subtotal"))
    vat = _as_dec(record.get("vat_amount"))
    ai_total = _as_dec(record.get("total"))
    # the document's raw TOTAL tokens are captured by the witness stream; the AI record
    # carries no raw lines. Reconciling the printed total is the witness's evidence, so
    # fall back to it whenever the primary record has none.
    tokens = ((record.get("total_raw") or {}).get("tokens")) or []
    if not tokens and corroboration is not None:
        tokens = ((corroboration.get("total_raw") or {}).get("tokens")) or []
    naive = parse_money(tokens[0]) if tokens else None
    concat = parse_money("".join(tokens)) if tokens else None

    # --- 1. the authority's inputs must be witnessed by BOTH reads -------------
    authority = None
    authority_trusted = True
    if subtotal is None or vat is None:
        authority_trusted = False
        v["ok"] = False
        v["failures"].append("subtotal_or_vat_unparsed")
    else:
        authority = (subtotal + vat).quantize(MONEY_TOLERANCE)
        if corroboration is not None:
            w_sub = _as_dec(corroboration.get("subtotal"))
            w_vat = _as_dec(corroboration.get("vat_amount"))
            if not (_money_close(subtotal, w_sub) and _money_close(vat, w_vat)):
                authority_trusted = False
                v["review_flags"].append(
                    f"authority_inputs_disagree: ai={subtotal}+{vat} witness={w_sub}+{w_vat}"
                )
    v["authority_inputs_agree"] = authority_trusted
    v["total_authority"] = authority

    # --- 2. reconcile the document's TOTAL line against the authority ----------
    if authority is None:
        total_status = "unparsed"
    elif not tokens:
        total_status = "no_total_line"  # nothing printed to reconcile against; arithmetic stands
    elif concat is not None and _money_close(concat, authority):
        total_status = "wrap_resolved" if (len(tokens) >= 2 and not _money_close(naive, authority)) else "clean"
    else:
        total_status = "mismatch"
        v["review_flags"].append(
            f"total_line_mismatch: document={concat!r} authority={authority}"
        )
    v["total_status"] = total_status
    v["wrap_detected"] = total_status == "wrap_resolved"
    v["total_agrees_with_ai"] = _money_close(ai_total, authority) if authority is not None else None

    # --- 3. publish a trusted total ONLY when everything lines up --------------
    safe = authority is not None and authority_trusted and total_status in {
        "clean", "wrap_resolved", "no_total_line",
    }
    record["total_trusted"] = authority if safe else None
    record["total_witness_ai"] = ai_total
    record["total_witness_regex_naive"] = naive

    # --- 4. line math ---------------------------------------------------------
    line_failures: list[int] = []
    for idx, item in enumerate(record.get("line_items") or []):
        qty = item.get("qty")
        unit = _as_dec(item.get("unit_price"))
        amount = _as_dec(item.get("amount"))
        if qty is None or unit is None or amount is None:
            line_failures.append(idx)
            continue
        if abs(Decimal(str(qty)) * unit - amount) > MONEY_TOLERANCE:
            line_failures.append(idx)
    v["line_math_ok"] = not line_failures
    v["line_math_failures"] = line_failures
    if line_failures:
        v["ok"] = False
        v["failures"].append(f"line_math_failed_rows={line_failures}")

    # --- 5. currency sanity ---------------------------------------------------
    currency = record.get("currency")
    currency = str(currency).strip().upper() or None if currency is not None else None
    record["currency"] = currency
    v["currency_known"] = currency in {"EUR", "GBP", "USD", "CHF"}
    if currency is not None and not v["currency_known"]:
        v["ok"] = False
        v["failures"].append(f"currency_unrecognised: {currency!r}")

    # --- 6. AI <-> regex agreement scoring ------------------------------------
    if corroboration is not None:
        agreement: dict[str, str] = {}
        for field in _COMPARED_FIELDS:
            agrees = _field_agrees(field, record.get(field), corroboration.get(field))
            agreement[field] = "agree" if agrees else "disagree"
            if not agrees:
                v["review_flags"].append(f"witness_disagreement: {field}")
        compared = len(_COMPARED_FIELDS)
        agreed = sum(1 for s in agreement.values() if s == "agree")
        v["agreement"] = agreement
        v["agreement_score"] = f"{agreed}/{compared}"
    else:
        v["agreement"] = None
        v["agreement_score"] = None

    record["verification"] = v
    return record
