"""FX normalisation tests — committed FX table -> EUR (fail closed on unknown currencies)."""
from decimal import Decimal

from src.fx import load_fx_table, to_eur, fx_as_of


def test_committed_fx_table_contains_eur_and_gbp():
    table = load_fx_table()
    assert table["EUR"] == Decimal("1.0")
    assert table["GBP"] == Decimal("1.1719")


def test_fx_as_of_date_is_committed():
    assert fx_as_of() == "2025-06-30"


def test_gbp_amount_converts_to_eur_with_rate_evidence():
    amount_eur, evidence = to_eur(Decimal("4998.00"), "GBP", load_fx_table())
    assert amount_eur == Decimal("5857.16")  # 4998.00 * 1.1719
    assert evidence["fx_known"] is True
    assert evidence["rate"] == Decimal("1.1719")
    assert evidence["as_of"] == "2025-06-30"
    assert evidence["amount_eur"] == Decimal("5857.16")


def test_eur_is_identity():
    amount_eur, evidence = to_eur(Decimal("8551.67"), "EUR", load_fx_table())
    assert amount_eur == Decimal("8551.67")
    assert evidence["fx_known"] is True


def test_unknown_currency_fails_closed():
    """No committed rate -> no conversion at all (P-E07: never a silent guess)."""
    amount_eur, evidence = to_eur(Decimal("100.00"), "CHF", load_fx_table())
    assert amount_eur is None
    assert evidence["fx_known"] is False
    assert evidence["amount_eur"] is None


def test_missing_amount_yields_no_eur_amount():
    amount_eur, evidence = to_eur(None, "GBP", load_fx_table())
    assert amount_eur is None
    assert evidence["fx_known"] is False
