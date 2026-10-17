"""S7/S8 — R5 duplicate & impostor detection: two deliberately different scopes (F8).

**R5b — metadata duplicates vs `historic_invoices.csv` (link L8).** VENDOR-SCOPED and
metadata-only, because the historic file has *no description column* (measured fact, §4.2-2).
Amount proximity and date-gap are reported as scored evidence; a duplicate flag is raised only
when the amount is inside the committed tolerance **and** the date-gap is inside the committed
window. Recurring deliveries (5–11 month gaps) therefore stay unflagged instead of drowning the
queue. Severity follows the historic status ladder PAID > APPROVED > PROCESSED (H7).

**R5a — within-batch semantic near-duplicates (link L7).** Cross-vendor descriptions are compared
by embedding cosine, then separated by two arithmetic signals:

1. *quantity arithmetic* — identical descriptions with **different** quantities are business-normal
   recurring services (the fuel trio), recorded as evidence only, never flagged;
2. *counterparty standing* — a **clone package** (same services, same quantities) only fires when one
   of the two counterparties is not payment-eligible (no master record, or INACTIVE). Two approved
   ACTIVE vendors quoting the same standard package is reported as "shared package" evidence, not a
   flag: a blind clone match would flag every framework-agreement pair in the fleet data.

Flagged pairs are escalated to a local LLM for a structured forensic verdict — the AI explains and
grades, the arithmetic gates decide (AI proposes, arithmetic disposes). A model grade may *raise* a
gate severity but can never lower it, and when the model is not reachable the same verdict is produced
deterministically, with `source` naming which path ran.

Embeddings: `requirements-extras.txt` installs `sentence-transformers`; when that is importable the
descriptions are encoded with `all-MiniLM-L6-v2`. Otherwise a dependency-free character n-gram cosine
is used, so the pipeline runs on `requirements.txt` alone. The backend that produced every score is
named in the evidence — a reader never has to guess.
"""
from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .llm_provider import LLMBadOutput, LLMUnavailable, chat_json

FORENSIC_PROMPT_VERSION = "ap-forensic-v1"

DEFAULT_HISTORIC_PATH = (
    Path(__file__).resolve().parents[2] / "ap-automation-assessment" / "historic_invoices.csv"
)
if not DEFAULT_HISTORIC_PATH.exists():
    DEFAULT_HISTORIC_PATH = (
        Path(__file__).resolve().parents[1] / "ap-automation-assessment" / "historic_invoices.csv"
    )

SEVERITY = {"PROCESSED": 1, "APPROVED": 2, "PAID": 3}

_RISK_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

# Rows the model sometimes captures from the totals block of a table — they are not services and
# must not participate in a service-package comparison.
_FOOTER_ROW = re.compile(
    r"^(sub\s*total|total|grand\s+total|vat|tax|amount\s+due|balance|net\s+amount|discount)\b"
)

FORENSIC_SYSTEM = (
    "You are a forensic accounts-payable analyst. Two invoices in the same batch share the same "
    "service descriptions. Decide whether the second looks like a cloned/discounted copy of the "
    "first from a vendor that is not payment-eligible, or a normal recurring service package. "
    "Answer only with the JSON object required by the schema."
)

FORENSIC_SCHEMA = {
    "type": "object",
    "properties": {
        "is_impostor_risk": {"type": "boolean"},
        "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
        "pattern": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["is_impostor_risk", "risk_level", "pattern", "explanation"],
}


# ---------------------------------------------------------------------------- historical records

@dataclass(frozen=True)
class HistoricInvoice:
    invoice_id: str
    vendor_id: str
    vendor_name: str
    invoice_date: str
    amount_eur: Decimal | None
    currency: str
    status: str
    po_number: str
    payment_reference: str


def load_historic(path: Path = DEFAULT_HISTORIC_PATH) -> list[HistoricInvoice]:
    """Read `historic_invoices.csv` -> list of HistoricInvoice.

    H3 (`vendor_name`) is carried for the audit trail but never used for matching — the matcher
    joins on H2 (`vendor_id`) only (H3 may disagree with the master; the data dictionary warns it).
    """
    rows: list[HistoricInvoice] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            invoice_id = (row.get("invoice_id") or "").strip()
            if not invoice_id:
                continue
            rows.append(HistoricInvoice(
                invoice_id=invoice_id,
                vendor_id=(row.get("vendor_id") or "").strip().upper(),
                vendor_name=(row.get("vendor_name") or "").strip(),
                invoice_date=(row.get("invoice_date") or "").strip(),
                amount_eur=_dec(row.get("amount_eur")),
                currency=(row.get("currency") or "").strip().upper(),
                status=(row.get("status") or "").strip().upper(),
                po_number=(row.get("po_number") or "").strip(),
                payment_reference=(row.get("payment_reference") or "").strip(),
            ))
    return rows


def check_historic(
    record: dict,
    historic: list[HistoricInvoice],
    *,
    amount_tolerance_pct: float = 2.0,
    date_window_days: int = 30,
) -> dict:
    """R5b — vendor-scoped metadata match against history. Scored evidence, never a silent verdict."""
    tolerance = Decimal(str(amount_tolerance_pct))
    invoice_number = str(record.get("invoice_number") or "").strip()
    po_number = str(record.get("po_number") or "").strip()
    vendor_id = ((record.get("vendor_match") or {}).get("vendor_id") or "").strip().upper()
    amount_eur = _dec((record.get("fx") or {}).get("amount_eur"))
    if amount_eur is None:
        amount_eur = _dec(record.get("total_trusted"))
    inv_date = _parse_date(record.get("invoice_date"))

    exact_matches: list[dict] = []
    for h in historic:
        if invoice_number and h.invoice_id and invoice_number.upper() == h.invoice_id.upper():
            exact_matches.append({"link": "P4==H1", "invoice_id": h.invoice_id,
                                  "status": h.status, "date": h.invoice_date})
        elif po_number and h.po_number and po_number.upper() == h.po_number.upper():
            exact_matches.append({"link": "P6==H8", "invoice_id": h.invoice_id,
                                  "status": h.status, "date": h.invoice_date})

    near_matches: list[dict] = []
    if vendor_id and amount_eur is not None and inv_date is not None:
        for h in historic:
            if h.vendor_id != vendor_id or not h.amount_eur:
                continue
            delta_pct = abs(amount_eur - h.amount_eur) / h.amount_eur * Decimal("100")
            gap = _gap_days(inv_date, h.invoice_date)
            near_matches.append({
                "invoice_id": h.invoice_id,
                "invoice_date": h.invoice_date,
                "amount_eur": h.amount_eur,
                "currency": h.currency,
                "amount_delta_pct": round(float(delta_pct), 4),
                "date_gap_days": gap,
                "status": h.status,
                "severity": SEVERITY.get(h.status, 0),
                "po_number": h.po_number,
                "within_tolerance": delta_pct <= tolerance,
                "within_window": gap is not None and gap <= date_window_days,
            })
        near_matches.sort(key=lambda m: (m["amount_delta_pct"],
                                         m["date_gap_days"] if m["date_gap_days"] is not None else 10**9))

    flagged = bool(exact_matches) or any(
        m["within_tolerance"] and m["within_window"] for m in near_matches
    )

    reason_code = None
    if exact_matches:
        first = exact_matches[0]
        reason_code = "R5b_exact_duplicate"
        detail = (f"same invoice/PO already in history ({exact_matches[0]['link']}, "
                  f"{first['invoice_id']}, status {exact_matches[0]['status']})")
    elif flagged:
        worst = next(m for m in near_matches if m["within_tolerance"] and m["within_window"])
        reason_code = "R5b_near_duplicate"
        detail = (f"same vendor, {worst['amount_delta_pct']}% amount delta and "
                  f"{worst['date_gap_days']}-day gap vs {worst['invoice_id']} ({worst['status']})")
    elif amount_eur is None:
        detail = "no verified EUR amount — historic comparison not possible (review the read)"
    else:
        detail = (f"no duplicate: {len(near_matches)} same-vendor historic rows, none inside "
                  f"{amount_tolerance_pct}% amount AND {date_window_days}-day window")

    return {
        "rule_id": "R5b",
        "status": "flag" if flagged else "pass",
        "reason_code": reason_code,
        "detail": detail,
        "vendor_history_count": sum(1 for h in historic if vendor_id and h.vendor_id == vendor_id),
        "exact_matches": exact_matches,
        "near_matches": near_matches[:5],
        "tolerance_pct": amount_tolerance_pct,
        "date_window_days": date_window_days,
    }


# ------------------------------------------------------------------------------------ helpers

def _dec(value) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if not text:
            return None
        if text.count(",") and "." in text:
            text = text.replace(",", "")
        elif text.count(",") == 1 and text.count(".") == 0:
            text = text.replace(",", ".")
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _gap_days(a: date | None, b_iso: str) -> int | None:
    b = _parse_date(b_iso)
    if a is None or b is None:
        return None
    return abs((a - b).days)


# ----------------------------------------------------------------------------------- embeddings

class Embedder:
    """Text -> vector. sentence-transformers when installed, dependency-free n-grams otherwise."""

    def __init__(self, backend: str = "auto") -> None:
        self.requested = backend
        self.name = "char4gram-cosine"
        self._model = None
        if backend in ("auto", "sentence-transformers"):
            try:  # opt-in heavy wheels (requirements-extras.txt) — absent by design on core install
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer("all-MiniLM-L6-v2")
                self.name = "sentence-transformers/all-MiniLM-L6-v2"
            except Exception:
                self._model = None

    def encode(self, text: str):
        if self._model is not None:
            vector = self._model.encode([text], normalize_embeddings=True)[0]
            return [float(x) for x in vector]
        return ngram_vector(text)


def normalise_text(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def ngram_vector(text: str, n: int = 4) -> dict[str, float]:
    padded = f" {normalise_text(text)} "
    counts: dict[str, float] = {}
    for i in range(len(padded) - n + 1):
        gram = padded[i:i + n]
        counts[gram] = counts.get(gram, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {g: v / norm for g, v in counts.items()}


def cosine(a, b) -> float:
    """Cosine similarity over sparse dict vectors (n-gram) or dense lists (MiniLM)."""
    if not a or not b:
        return 0.0
    if isinstance(a, dict):
        if len(a) > len(b):
            a, b = b, a
        return float(sum(v * b.get(k, 0.0) for k, v in a.items()))
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return float(num / (na * nb))


def service_line_items(record: dict) -> list[dict]:
    """Line items that are actual services (totals/VAT rows captured by the reader are dropped)."""
    out: list[dict] = []
    for item in record.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description or _FOOTER_ROW.match(normalise_text(description)):
            continue
        out.append(item)
    return out


def description_document(record: dict) -> str:
    """The text the similarity layer sees: the service descriptions, one per line."""
    return " \n ".join(str(item.get("description") or "") for item in service_line_items(record))


def align_line_items(items_a: list[dict], items_b: list[dict]) -> list[tuple[dict, dict]]:
    """Greedy one-to-one alignment of line items by description similarity."""
    remaining = list(range(len(items_b)))
    aligned: list[tuple[dict, dict]] = []
    for item_a in items_a:
        vec_a = ngram_vector(item_a.get("description") or "")
        best_idx, best_score = None, -1.0
        for idx in remaining:
            score = cosine(vec_a, ngram_vector(items_b[idx].get("description") or ""))
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is None:
            continue
        remaining.remove(best_idx)
        aligned.append((item_a, items_b[best_idx]))
    return aligned


def quantity_match(aligned: list[tuple[dict, dict]]) -> float:
    """Mean of min(qa,qb)/max(qa,qb) over aligned services. 1.0 = identical quantities."""
    ratios: list[float] = []
    for item_a, item_b in aligned:
        qa, qb = _dec(item_a.get("qty")), _dec(item_b.get("qty"))
        if qa is None or qb is None:
            continue
        hi = max(qa, qb)
        if hi == 0:
            ratios.append(1.0 if qa == qb else 0.0)
        elif min(qa, qb) < 0:
            ratios.append(0.0)
        else:
            ratios.append(float(min(qa, qb) / hi))
    return sum(ratios) / len(ratios) if ratios else 0.0


def unit_price_ratio(aligned: list[tuple[dict, dict]]) -> float | None:
    """Median of unit_price_b / unit_price_a over aligned services (the price-scaling signal)."""
    ratios: list[float] = []
    for item_a, item_b in aligned:
        pa, pb = _dec(item_a.get("unit_price")), _dec(item_b.get("unit_price"))
        if pa is None or pb is None or pa <= 0 or pb <= 0:
            continue
        ratios.append(float(pb / pa))
    if not ratios:
        return None
    ratios.sort()
    mid = len(ratios) // 2
    return ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2.0


# ------------------------------------------------------------------------------ within-batch R5a

def vendor_key(record: dict) -> str:
    """Identity used to decide whether two invoices come from different vendors."""
    vendor_id = ((record.get("vendor_match") or {}).get("vendor_id") or "").strip()
    if vendor_id:
        return vendor_id.upper()
    return f"name:{normalise_text(record.get('vendor_name'))}"


def counterparty_risk(record: dict) -> tuple[bool, str]:
    """Is this counterparty payment-eligible? Returns (risky, human label)."""
    match_status = ((record.get("vendor_match") or {}).get("status") or "").lower()
    master_status = str((record.get("vendor_master") or {}).get("status") or "").upper()
    if match_status == "unknown" or not (record.get("vendor_match") or {}).get("vendor_id"):
        return True, "unknown vendor (no master record)"
    if master_status == "INACTIVE":
        return True, "INACTIVE vendor (not eligible to process)"
    return False, master_status or "master status unknown"


@dataclass
class PairEvidence:
    a_invoice: str
    b_invoice: str
    a_vendor: str
    b_vendor: str
    a_vendor_id: str | None
    b_vendor_id: str | None
    a_standing: str
    b_standing: str
    cross_vendor: bool
    description_cosine: float
    quantity_match: float
    line_count_equal: bool
    unit_price_ratio: float | None
    pattern: str
    flagged: bool
    backend: str
    paired_services: list[dict]

    def to_dict(self) -> dict:
        return {
            "a_invoice": self.a_invoice,
            "b_invoice": self.b_invoice,
            "a_vendor": self.a_vendor,
            "b_vendor": self.b_vendor,
            "a_vendor_id": self.a_vendor_id,
            "b_vendor_id": self.b_vendor_id,
            "a_standing": self.a_standing,
            "b_standing": self.b_standing,
            "cross_vendor": self.cross_vendor,
            "description_cosine": round(self.description_cosine, 4),
            "quantity_match": round(self.quantity_match, 4),
            "line_count_equal": self.line_count_equal,
            "unit_price_ratio": round(self.unit_price_ratio, 4) if self.unit_price_ratio else self.unit_price_ratio,
            "pattern": self.pattern,
            "flagged": self.flagged,
            "embedding_backend": self.backend,
            "paired_services": self.paired_services,
        }


def find_within_batch_pairs(
    records: list[dict],
    embedder: Embedder | None = None,
    *,
    cosine_threshold: float = 0.85,
    quantity_match_threshold: float = 0.95,
    evidence_floor: float = 0.60,
    max_services: int = 6,
) -> list[PairEvidence]:
    """R5a — every cross-invoice pair whose service descriptions are close, scored and classified.

    Pairs *below* the flag gate are still returned (evidence_floor allows the reader to see the
    near-misses, e.g. the fuel trio) — evidence is published, flags are earned.
    """
    emb = embedder or Embedder()
    vectors = [emb.encode(description_document(r)) for r in records]
    items = [service_line_items(r) for r in records]
    pairs: list[PairEvidence] = []

    for i in range(len(records)):
        if not items[i]:
            continue
        for j in range(i + 1, len(records)):
            if not items[j]:
                continue
            score = cosine(vectors[i], vectors[j])
            if score < evidence_floor:
                continue
            aligned = align_line_items(items[i], items[j])
            q_match = quantity_match(aligned)
            counts_equal = len(items[i]) == len(items[j])
            risky_a, label_a = counterparty_risk(records[i])
            risky_b, label_b = counterparty_risk(records[j])
            cross_vendor = vendor_key(records[i]) != vendor_key(records[j])
            risky = risky_a or risky_b

            clone_signature = (
                cross_vendor
                and score >= cosine_threshold
                and counts_equal
                and q_match >= quantity_match_threshold
            )
            flagged = bool(clone_signature and risky)
            if flagged:
                pattern = "CLONE_PACKAGE_BLOCKED_VENDOR"
            elif clone_signature:
                pattern = "SHARED_PACKAGE_APPROVED_VENDORS"
            elif score >= cosine_threshold:
                pattern = "SHARED_DESCRIPTIONS_DIFFERENT_QUANTITY"
            else:
                pattern = "SHARED_DESCRIPTIONS"

            services = [
                {
                    "description": str(a.get("description") or ""),
                    "qty_a": a.get("qty"), "qty_b": b.get("qty"),
                    "unit_a": a.get("unit_price"), "unit_b": b.get("unit_price"),
                }
                for a, b in aligned[:max_services]
            ]
            pairs.append(PairEvidence(
                a_invoice=_invoice_label(records[i]),
                b_invoice=_invoice_label(records[j]),
                a_vendor=str(records[i].get("vendor_name") or "?"),
                b_vendor=str(records[j].get("vendor_name") or "?"),
                a_vendor_id=(records[i].get("vendor_match") or {}).get("vendor_id"),
                b_vendor_id=(records[j].get("vendor_match") or {}).get("vendor_id"),
                a_standing=label_a,
                b_standing=label_b,
                cross_vendor=cross_vendor,
                description_cosine=score,
                quantity_match=q_match,
                line_count_equal=counts_equal,
                unit_price_ratio=unit_price_ratio(aligned),
                pattern=pattern,
                flagged=flagged,
                backend=emb.name,
                paired_services=services,
            ))

    pairs.sort(key=lambda p: (not p.flagged, -p.description_cosine, p.a_invoice))
    return pairs


def pair_rule(pair: PairEvidence) -> dict:
    """Rule-shaped result for the disposition ladder (R5a)."""
    if pair.flagged:
        detail = (
            f"{pair.b_vendor} ({pair.b_standing}) invoices the same {len(pair.paired_services)} "
            f"services as {pair.a_vendor} with matching quantities (match {pair.quantity_match:.2f}); "
            f"unit prices at {_ratio_text(pair.unit_price_ratio)} of {pair.a_vendor}'s — "
            f"clone-package evidence"
        )
        return {"rule_id": "R5a", "status": "flag", "reason_code": "R5a_clone_package",
                "detail": detail, "pattern": pair.pattern,
                "counterparty": pair.b_invoice}
    detail = (
        f"no clone flag: {pair.a_vendor} vs {pair.b_vendor} — cosine {pair.description_cosine:.3f}, "
        f"quantity match {pair.quantity_match:.2f}, prices {_ratio_text(pair.unit_price_ratio)} "
        f"({pair.pattern})"
    )
    return {"rule_id": "R5a", "status": "pass", "reason_code": None,
            "detail": detail, "pattern": pair.pattern, "counterparty": pair.b_invoice}


def deterministic_verdict(pair: PairEvidence) -> dict:
    """Gate verdict — the operative severity, derived from the measured evidence only.

    Payment eligibility is a deterministic fact, so severity is owned here: a counterparty that is
    not payment-eligible (unknown vendor or INACTIVE) makes a clone package CRITICAL.
    """
    risk = _gate_risk(pair)
    explanation = (
        f"{pair.b_vendor} ({pair.b_standing}) bills the same services with the same quantities as "
        f"{pair.a_vendor}, with its unit prices scaled to {_ratio_text(pair.unit_price_ratio)} of "
        f"{pair.a_vendor}'s. A re-priced clone of an approved charge package from a counterparty "
        f"that is not payment-eligible — verify with the requesting department before paying."
    )
    return {
        "source": "deterministic",
        "model": None,
        "prompt_version": None,
        "latency_ms": None,
        "risk_level": risk,          # operative severity — owned by the arithmetic gates
        "gate_risk_level": risk,
        "ai_risk_level": None,
        "is_impostor_risk": True,    # a flagged pair is a clone candidate by construction
        "ai_is_impostor_risk": None,
        "pattern": "CLONE_PACKAGE_DIFFERENT_COUNTERPARTY",
        "explanation": explanation,
        "gate_explanation": explanation,
        "ai_explanation": None,
    }


def _gate_risk(pair: PairEvidence) -> str:
    blocked = ("unknown vendor" in pair.a_standing or "unknown vendor" in pair.b_standing
               or "INACTIVE" in pair.a_standing or "INACTIVE" in pair.b_standing)
    return "CRITICAL" if blocked else "HIGH"


def forensic_verdict(
    pair: PairEvidence,
    record_a: dict,
    record_b: dict,
    llm_cfg: dict | None = None,
) -> dict:
    """Structured LLM verdict for a flagged pair; deterministic fallback when AI is unavailable.

    The model may *raise* the severity but can never lower it: `risk_level` is the gate severity
    raised to the model's grade when the model is more severe, and the model's own grade is kept
    verbatim as `ai_risk_level`. Measured on the batch, a 3B model graded both confirmed clone pairs
    LOW while its own prose described a cloned invoice — exactly the judgement the gate must not
    hand over.
    """
    gate = deterministic_verdict(pair)
    if not llm_cfg:
        return gate
    try:
        result = chat_json(
            FORENSIC_SYSTEM,
            _forensic_prompt(pair, record_a, record_b),
            FORENSIC_SCHEMA,
            model=llm_cfg["model"],
            prompt_version=FORENSIC_PROMPT_VERSION,
            temperature=llm_cfg.get("temperature", 0.0),
            base_url=llm_cfg["base_url"],
        )
    except (LLMUnavailable, LLMBadOutput):
        return gate

    data = result.data
    ai_risk = str(data.get("risk_level") or "").upper()
    if ai_risk not in _RISK_ORDER:
        ai_risk = None
    operative = gate["risk_level"]
    if ai_risk and _RISK_ORDER[ai_risk] > _RISK_ORDER[operative]:
        operative = ai_risk

    verdict = dict(gate)
    verdict.update({
        "source": "llm",
        "model": result.model,
        "prompt_version": result.prompt_version,
        "latency_ms": result.latency_ms,
        "risk_level": operative,
        "ai_risk_level": ai_risk,
        "ai_is_impostor_risk": bool(data.get("is_impostor_risk", False)),
        "pattern": str(data.get("pattern") or gate["pattern"])[:200],
        "explanation": str(data.get("explanation") or gate["explanation"])[:600],
        "ai_explanation": str(data.get("explanation") or "")[:600] or None,
    })
    return verdict


# ------------------------------------------------------------------------------- prompt support

def _forensic_prompt(pair: PairEvidence, record_a: dict, record_b: dict) -> str:
    def _block(label: str, record: dict, standing: str) -> str:
        lines = "\n".join(
            f"    - {item.get('description')} | qty={item.get('qty')} | unit={item.get('unit_price')}"
            for item in service_line_items(record)
        )
        return (
            f"Invoice {label}: {record.get('invoice_number')} | vendor={record.get('vendor_name')} "
            f"| vendor_id={(record.get('vendor_match') or {}).get('vendor_id') or 'NO MASTER RECORD'} "
            f"| standing={standing} | date={record.get('invoice_date')} | "
            f"currency={record.get('currency')} | stated_total={record.get('total')}\n"
            f"  services:\n{lines}"
        )

    return (
        "Two invoices in the same batch share service descriptions. Measured evidence:\n\n"
        f"{_block('A', record_a, pair.a_standing)}\n\n"
        f"{_block('B', record_b, pair.b_standing)}\n\n"
        f"Measured similarity: description cosine={pair.description_cosine:.3f}, "
        f"quantity match={pair.quantity_match:.2f}, unit-price ratio B/A={_ratio_text(pair.unit_price_ratio)}.\n\n"
        "Is B a cloned/discounted copy of A's charge package (impostor / duplicate-payment risk), "
        "or a normal recurring service package? Consider the vendor standing. "
        "Answer with the JSON object only: is_impostor_risk, risk_level, pattern, explanation."
    )


def _invoice_label(record: dict) -> str:
    return str(record.get("invoice_number") or record.get("source_file") or "?")


def _ratio_text(ratio: float | None) -> str:
    if ratio is None:
        return "n/a"
    return f"{ratio * 100:.1f}%"
