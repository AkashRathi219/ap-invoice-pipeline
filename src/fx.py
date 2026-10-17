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
    """Reads the static committed FX rates table from config/fx_rates.csv.

    Args:
        path: Path to the fx_rates.csv configuration file.

    Returns:
        Dict mapping ISO currency strings to Decimal conversion rates to EUR (e.g. {'GBP': Decimal('1.1719')}).

    Where called:
        Called by run.py at initialization, and by tests/test_fx.py.

    Where it sends information:
        Supplies fx_table to to_eur() for normalising foreign currency invoices.
    """
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
    """Retrieves the latest committed as-of audit date recorded in fx_rates.csv.

    Args:
        path: Path to fx_rates.csv.

    Returns:
        ISO date string representing the effective date of the FX rates.

    Where called:
        Called by run.py at startup to attach audit timestamps to currency evidence.

    Where it sends information:
        Recorded in the 'fx' block of each invoice record and printed in output reports.
    """
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
    """Converts a monetary amount from its native currency into EUR using the static rate table.

    Implements fail-closed conversion: if the currency is unrecognized or missing a committed rate,
    returns amount_eur=None and fx_known=False. The pipeline refuses to guess exchange rates.

    Args:
        amount: Raw or trusted numeric amount.
        currency: ISO currency code (EUR, GBP, etc.).
        fx_table: Conversion table from load_fx_table().
        as_of: Audit effective date.

    Returns:
        Tuple of (converted_amount_eur, evidence_dict).

    Where called:
        Called by run.py in Pass 1 after arithmetic verification.

    Where it sends information:
        Populates record['fx']. amount_eur is passed directly to rules.py (run_rules())
        for R3 spend threshold evaluation, and to duplicates.py (check_historic()) for R5b.
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
    """Helper to ensure currency values are converted to Decimal."""
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
