"""S5 — Vendor master link L1 -> L2 -> L3: exact, normalised-fuzzy, country corroboration.

L1: exact name match against the vendor master (Unicode-normalised, case-insensitive).
L2: fuzzy name match (token/sequence similarity >= ``fuzzy_threshold``, default 0.85) for
     diacritic / spacing / casing variants (e.g. `QuickFix OÜ` vs `QuickFix OAo`).
L3: the master country corroborates the invoice country; disagreements are flagged, never
     silently resolved.

The master carries NO VAT column (M1/M2 only), so the VAT string is corroboration only and
never used to resolve the vendor identity.
"""
from __future__ import annotations

import csv
import difflib
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

FUZZY_THRESHOLD_DEFAULT = 0.85
DEFAULT_MASTER_PATH = Path(__file__).resolve().parents[1].resolve().parent / "ap-automation-assessment" / "vendor_master.csv"
if not DEFAULT_MASTER_PATH.exists():
    DEFAULT_MASTER_PATH = Path(__file__).resolve().parents[1] / "ap-automation-assessment" / "vendor_master.csv"
if not DEFAULT_MASTER_PATH.exists():
    DEFAULT_MASTER_PATH = Path(__file__).resolve().parents[2] / "ap-automation-assessment" / "vendor_master.csv"


@dataclass(frozen=True)
class VendorRow:
    vendor_id: str
    vendor_name: str
    category: str | None
    payment_terms_days: int | None
    approved_currency: str | None
    spend_threshold_eur: Decimal | None
    status: str
    country: str | None
    contact_email: str | None
    requires_dual_approval: bool


@dataclass(frozen=True)
class VendorMatch:
    vendor_name: str
    status: str  # "exact" | "fuzzy" | "unknown"
    vendor_id: str | None
    vendor_name_matched: str | None
    fuzzy_score: float | None
    country_match: bool | None
    vendor_row: VendorRow | None
    reasons: tuple[str, ...]


def normalise_name(value) -> str:
    """Case-fold + strip diacritics + collapse whitespace -> canonical vendor-key form."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def normalise_country(value) -> str:
    return " ".join(str(value or "").strip().upper().split())


def load_vendor_master(path: Path = DEFAULT_MASTER_PATH) -> list[VendorRow]:
    """Read the committed vendor master -> list of VendorRow."""
    rows: list[VendorRow] = []
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            vendor_id = (row.get("vendor_id") or "").strip()
            if not vendor_id:
                continue
            rows.append(VendorRow(
                vendor_id=vendor_id,
                vendor_name=(row.get("vendor_name") or "").strip(),
                category=(row.get("category") or "").strip() or None,
                payment_terms_days=_int_or_none(row.get("payment_terms_days")),
                approved_currency=(row.get("approved_currency") or "").strip().upper() or None,
                spend_threshold_eur=_dec_or_none(row.get("spend_threshold_eur")),
                status=(row.get("status") or "").strip().upper(),
                country=(row.get("country") or "").strip() or None,
                contact_email=(row.get("contact_email") or "").strip() or None,
                requires_dual_approval=str(row.get("requires_dual_approval") or "").strip().upper() in {"TRUE", "1", "YES"},
            ))
    return rows


def match_vendor(
    vendor_name,
    vendor_country,
    master_rows: list[VendorRow],
    fuzzy_threshold: float = FUZZY_THRESHOLD_DEFAULT,
) -> VendorMatch:
    """L1 exact -> L2 normalised-fuzzy -> L3 country corroboration."""
    key = normalise_name(vendor_name)
    inv_country = normalise_country(vendor_country)

    exact_row = next(
        (r for r in master_rows if r.vendor_name and normalise_name(r.vendor_name) == key),
        None,
    )
    if exact_row is not None:
        return VendorMatch(
            vendor_name=str(vendor_name or ""),
            status="exact",
            vendor_id=exact_row.vendor_id,
            vendor_name_matched=exact_row.vendor_name,
            fuzzy_score=1.0,
            country_match=_country_agrees(inv_country, exact_row.country),
            vendor_row=exact_row,
            reasons=(),
        )

    best_score = 0.0
    best_row: VendorRow | None = None
    for row in master_rows:
        if not row.vendor_name:
            continue
        score = difflib.SequenceMatcher(None, key, normalise_name(row.vendor_name)).ratio()
        if score > best_score:
            best_score, best_row = score, row
    if best_row is not None and best_score >= fuzzy_threshold:
        return VendorMatch(
            vendor_name=str(vendor_name or ""),
            status="fuzzy",
            vendor_id=best_row.vendor_id,
            vendor_name_matched=best_row.vendor_name,
            fuzzy_score=best_score,
            country_match=_country_agrees(inv_country, best_row.country),
            vendor_row=best_row,
            reasons=("R1_fuzzy_vendor_match",),
        )
    return VendorMatch(
        vendor_name=str(vendor_name or ""),
        status="unknown",
        vendor_id=None,
        vendor_name_matched=None,
        fuzzy_score=None,
        country_match=None,
        vendor_row=None,
        reasons=("R1_unknown_vendor",),
    )


def _country_agrees(inv_country: str, row_country: str | None) -> bool | None:
    if not inv_country or not row_country:
        return None
    return inv_country == normalise_country(row_country)


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _dec_or_none(value) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except Exception:
        return None
