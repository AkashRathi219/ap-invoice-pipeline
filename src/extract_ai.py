"""S3-AI — PRIMARY document understanding: schema-constrained extraction, every invoice (17/17).

DEVELOPMENT_PLAN.md A1: this is the pipeline's main reader. The model is told to COPY
values exactly as printed (never compute) — recomputation is verification's job (S4).
A page-wrapped total hint is legitimate prompt engineering: arithmetic authority still
verifies whatever the model joins together.

Output contract matches the P1..P14 field set so the regex corroboration stream and the
verification layer consume identical shapes.
"""
from __future__ import annotations

from decimal import Decimal

from .llm_provider import LLMResult, chat_json

PROMPT_VERSION = "ap-extract-v1"

SYSTEM_PROMPT = (
    "You are an invoice field-extraction engine used in accounts payable. "
    "Output only the JSON object required by the schema."
)

USER_TEMPLATE = """Extract every field from this invoice text.

Rules:
- Copy every value exactly as printed. Never recompute, round, correct or complete anything.
- If a number is split across two lines by a page-wrap (e.g. a total shown as '€4' followed by
  '2,121.24' on the next line), join the digits into one number (42121.24).
- Amounts and unit prices: JSON numbers with the printed decimals (8551.67). Quantities: integers.
- currency: the ISO code printed on the document: EUR, GBP, USD or CHF.
- invoice_date: as YYYY-MM-DD.
- po_number: null when the document shows no PO reference.
- free_text_note: the trailing vendor note text if present, else null.
- payment: iban / bic / bank exactly as printed; null for absent parts.
- extraction_confidence: your confidence across all fields, 0.0-1.0.

Invoice text:
{text}
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "required": [
        "vendor_name", "vendor_vat", "vendor_address", "vendor_country",
        "invoice_number", "invoice_date", "po_number", "bill_to",
        "line_items", "subtotal", "vat_amount", "total", "currency",
        "payment", "free_text_note", "extraction_confidence",
    ],
    "properties": {
        "vendor_name": {"type": ["string", "null"]},
        "vendor_vat": {"type": ["string", "null"]},
        "vendor_address": {"type": ["string", "null"]},
        "vendor_country": {"type": ["string", "null"]},
        "invoice_number": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"]},
        "po_number": {"type": ["string", "null"]},
        "bill_to": {"type": ["string", "null"]},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["description", "qty", "unit_price", "amount"],
                "properties": {
                    "description": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number"},
                },
            },
        },
        "subtotal": {"type": ["number", "null"]},
        "vat_amount": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
        "payment": {
            "type": "object",
            "required": ["iban", "bic", "bank"],
            "properties": {
                "iban": {"type": ["string", "null"]},
                "bic": {"type": ["string", "null"]},
                "bank": {"type": ["string", "null"]},
            },
        },
        "free_text_note": {"type": ["string", "null"]},
        "extraction_confidence": {"type": "number"},
    },
}

_MONEY_KEYS = ("subtotal", "vat_amount", "total")
_ITEM_MONEY_KEYS = ("unit_price", "amount")


def _dec(value) -> Decimal | None:
    """JSON number -> Decimal without float artifacts (str() round-trip)."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _normalise(data: dict) -> dict:
    items = []
    for raw in data.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        items.append({
            "description": raw.get("description"),
            "qty": raw.get("qty"),
            "unit_price": _dec(raw.get("unit_price")),
            "amount": _dec(raw.get("amount")),
        })
    payment = data.get("payment") or {}
    return {
        "vendor_name": data.get("vendor_name"),
        "vendor_vat": data.get("vendor_vat"),
        "vendor_address": data.get("vendor_address"),
        "vendor_country": data.get("vendor_country"),
        "invoice_number": data.get("invoice_number"),
        "invoice_date": data.get("invoice_date"),
        "po_number": data.get("po_number"),
        "bill_to": data.get("bill_to"),
        "line_items": items,
        "subtotal": _dec(data.get("subtotal")),
        "vat_amount": _dec(data.get("vat_amount")),
        "total": _dec(data.get("total")),
        "currency": (data.get("currency") or None),
        "payment": {
            "iban": payment.get("iban"),
            "bic": payment.get("bic"),
            "bank": payment.get("bank"),
        },
        "free_text_note": data.get("free_text_note"),
        "extraction_confidence": data.get("extraction_confidence"),
    }


def extract_invoice_ai(
    text: str,
    source_name: str,
    model: str,
    base_url: str,
    temperature: float = 0.0,
    prompt_version: str = PROMPT_VERSION,
    max_chars: int = 8000,
) -> dict:
    """Primary extraction: AI reads the raw text; provenance and telemetry travel with the record."""
    result: LLMResult = chat_json(
        system=SYSTEM_PROMPT,
        user=USER_TEMPLATE.format(text=text[:max_chars]),
        schema=EXTRACT_SCHEMA,
        model=model,
        prompt_version=prompt_version,
        temperature=temperature,
        base_url=base_url,
    )
    record = _normalise(result.data)
    record.update({
        "source_file": source_name,
        "extraction_layer": "ai_primary",
        "model": result.model,
        "prompt_version": result.prompt_version,
        "latency_ms": result.latency_ms,
        "tokens": {
            "prompt": result.prompt_tokens,
            "completion": result.completion_tokens,
        },
    })
    return record


def extract_invoice_fallback_regex(text: str, source_name: str, regex_record: dict) -> dict:
    """Named offline fallback: promote the corroboration stream when no LLM is reachable (P-E01)."""
    record = dict(regex_record)
    record.update({
        "source_file": source_name,
        "extraction_layer": "regex_fallback",
        "model": None,
        "prompt_version": None,
        "latency_ms": None,
        "tokens": None,
        "extraction_confidence": None,
    })
    return record
