"""S4 — FX normalisation: committed ``fx_rates.csv`` -> EUR amounts (GBP/CHF/USD -> EUR).

The spend-threshold comparison (L5) and the historic-duplicate amount-proximity
comparison (L8) are ALWAYS executed in EUR. Non-EUR invoices must therefore be
converted with a *committed, dated* rate before any EUR comparison happens —
never a silent self-conversion.

Fail-closed design (P-E07): a currency with no committed rate converts to
``None``. Nothing downstream may guess an exchange rate, because a silently
guessed rate would be a silently wrong EUR amount.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

CENT = Decimal("0.01")
DEFAULT_RATES_PATH = Path(__file__).resolve().parents[1] / "config" / "fx_rates.csv"


def load_fx_table(path: Path = DEFAULT_RATES_PATH) -> dict[str, Decimal]:
    """Read the committed FX config file -> ``{ISO currency: rate_to_eur}``."""
    table: dict[str, Decimal] = {}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = []
    for l in text.splitlines():
        stripped = l.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(l)
    if not lines:
        return table
    with io.StringIO("\n".join(lines)) as fh:
        for row in csv.DictReader(fh):
            if not row:
                continue
            currency = (row.get("currency") or "").strip().upper()
            if not currency:
                continue
            raw_rate = (row.get("rate_to_eur") or "").strip()
            if not raw_rate:
                continue
            try:
                rate = Decimal(raw_rate)
            except Exception:
                continue
            if rate > 0:
                table[currency] = rate
    return table


def fx_as_of(path: Path = DEFAULT_RATES_PATH) -> str:
    """Return the committed as-of date of the FX table (latest row wins)."""
    as_of = ""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = []
    for l in text.splitlines():
        stripped = l.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(l)
    if not lines:
        return as_of
    with io.StringIO("\n".join(lines)) as fh:
        for row in csv.DictReader(fh):
            if not row:
                continue
            candidate = (row.get("as_of") or "").strip()
            if candidate and candidate > as_of:
                as_of = candidate
    return as_of


def to_eur(amount, currency, fx_table: dict[str, Decimal], as_of: str | None = None) -> tuple[Decimal | None, dict]:
    """Convert ``amount`` of ``currency`` into EUR.

    Returns ``(amount_eur, evidence)``. ``amount_eur`` is ``None`` when the
    amount or the currency is missing, or when the currency has no committed
    rate — in which case ``evidence['fx_known']`` is ``False`` and the caller
    must treat the invoice as not-yet-clearable instead of guessing.
    """
    currency_code = str(currency).strip().upper() if currency is not None else "EUR"
    if not currency_code:
        currency_code = "EUR"
    evidence: dict = {
        "amount": _as_decimal(amount),
        "currency": currency_code,
        "rate": None,
        "as_of": as_of if as_of is not None else fx_as_of(),
        "fx_known": False,
        "amount_eur": None,
    }
    value = _as_decimal(amount)
    if value is None:
        return None, evidence
    rate = fx_table.get(currency_code)
    if rate is None or rate <= 0:
        # fail closed — never silently convert with an un-committed rate
        return None, evidence
    amount_eur = (value * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    evidence["rate"] = rate
    evidence["fx_known"] = True
    evidence["amount_eur"] = amount_eur
    return amount_eur, evidence


def _as_decimal(value) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except Exception:
            return None
    return None
