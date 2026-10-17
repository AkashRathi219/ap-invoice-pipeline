"""S3-REGEX — corroboration evidence stream (DEVELOPMENT_PLAN.md A1, G7).

This is NO LONGER the pipeline's reader. It runs in parallel with the AI extraction on
every invoice so that (a) each AI field has an independent deterministic witness, and
(b) the pipeline degrades to this layer with named provenance if Ollama is down.

It captures the TOTAL line RAW (same-line token + optional next-line token): the wrap
decision belongs to verification (arithmetic authority), never to a parsing layer.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz  # PyMuPDF

MONEY_TOKEN_RE = re.compile(r"^[-+]?[\d,]+(?:\.\d+)?$")
MONEYISH_TOKEN_RE = re.compile(r"^[€£$]?[\d,]+(?:\.\d+)?$")
SYMBOL_CURRENCY = {"€": "EUR", "£": "GBP", "$": "USD"}
CURRENCY_CODE_RE = re.compile(r"\b(EUR|GBP|USD|CHF)\b")

LABELS = {
    "A": {"number": "Invoice Number:", "date": "Invoice Date:", "po": "PO Reference:"},
    "B": {"number": "Invoice No:", "date": "Date:", "po": "PO Ref:"},
}


class ExtractionError(Exception):
    """No known label dictionary matched — AI still handles it; corroboration reports honestly."""


def read_pdf_text(path: Path) -> str:
    """Extracts raw plain text stream from all pages of a PDF document using PyMuPDF (fitz).

    Args:
        path: Filepath to target PDF.

    Returns:
        Joined multiline string of extracted page text.

    Where called:
        Called by run.py in the Pass 1 per-invoice loop before launching extractors.

    Where it sends information:
        Sent to both extract_invoice_ai() and extract_invoice_regex() for processing.
    """
    with fitz.open(path) as doc:
        return "\n".join(page.get_text("text") for page in doc)


def split_lines(text: str) -> list[str]:
    """Cleans up raw text, normalises non-breaking spaces, and strips whitespace from non-empty lines."""
    text = text.replace("\\n", "\n").replace("\u00a0", " ")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def parse_money(token: str | None) -> Decimal | None:
    """Parses raw text currency representations into exact Python Decimal objects.

    Strips symbols (€, £, $), ISO codes (EUR, GBP), commas, and spaces. Money is never float.
    """
    if token is None:
        return None
    t = token.strip()
    for sym in SYMBOL_CURRENCY:
        t = t.replace(sym, "")
    t = CURRENCY_CODE_RE.sub("", t).strip().replace(",", "").replace(" ", "")
    if not MONEY_TOKEN_RE.match(t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _value_after(lines: list[str], label: str) -> str | None:
    """Extracts string value following a specific header label prefix in a line."""
    for ln in lines:
        if ln.startswith(label):
            value = ln[len(label):].strip()
            return value or None
    return None


def detect_format(lines: list[str]) -> str | None:
    """Detects layout class: 'A' (Standard structured digital) vs 'B' (Compact scan-style)."""
    if any(ln.startswith("Invoice Number:") for ln in lines):
        return "A"
    if any(ln.startswith("Invoice No:") for ln in lines):
        return "B"
    return None


def extract_invoice_regex(text: str, source_name: str) -> dict:
    """Performs deterministic second-read extraction over PDF text using pattern matching.

    Extracts vendor identity, dates, PO, line-item chunks (desc, qty, unit, amt), raw total
    tokens (preserving line breaks for wrap detection), currency signals, and banking details.

    Args:
        text: Raw text string from read_pdf_text().
        source_name: Name of PDF file for diagnostic messages.

    Returns:
        Dict representation of extracted fields tagged as 'regex_corroboration'.

    Where called:
        Called by run.py in Pass 1 as the secondary witness read, and by test_extract_regex.py.

    Where it sends information:
        Sent to validate_extract.py (verify()) for arithmetic reconciliation against the AI read,
        and used as fallback input in extract_invoice_fallback_regex() if Ollama is unavailable.
    """
    lines = split_lines(text)
    fmt = detect_format(lines)
    if fmt is None:
        raise ExtractionError(f"{source_name}: no known invoice-number label found")

    warnings: list[str] = []
    rec: dict = {
        "source_file": source_name,
        "format_tag": fmt,
        "extraction_layer": "regex_corroboration",
    }

    # vendor block (everything before the INVOICE marker)
    if "INVOICE" not in lines:
        raise ExtractionError(f"{source_name}: INVOICE marker missing")
    inv_idx = lines.index("INVOICE")
    block = lines[:inv_idx]
    rec["vendor_name"] = block[0] if block else None
    rec["vendor_address"] = block[1] if len(block) > 1 else None
    locality = block[2] if len(block) > 2 else None
    rec["vendor_country"] = (
        locality.rsplit(",", 1)[-1].strip() if locality and "," in locality else None
    )
    rec["vendor_vat"] = next(
        (ln.split(":", 1)[1].strip() for ln in block if ln.startswith("VAT:")), None
    )

    # header fields via the format's label dictionary
    labels = LABELS[fmt]
    rec["invoice_number"] = _value_after(lines, labels["number"])
    rec["invoice_date"] = _value_after(lines, labels["date"])
    rec["po_number"] = _value_after(lines, labels["po"])
    if rec["po_number"] is None:
        warnings.append("po_number_absent")

    # bill-to: inline (B) or a block until Description (A)
    bill_line = next((ln for ln in lines if ln.startswith("Bill To:")), None)
    inline = bill_line.split(":", 1)[1].strip() if bill_line else None
    if inline:
        rec["bill_to"] = inline
    elif bill_line and "Description" in lines:
        start = lines.index(bill_line) + 1
        end = lines.index("Description")
        rec["bill_to"] = ", ".join(lines[start:end]) or None
    else:
        rec["bill_to"] = None

    # table + line items in fixed 4-line chunks (desc / qty / unit / amount)
    if "Description" not in lines:
        raise ExtractionError(f"{source_name}: table header missing")
    di = lines.index("Description")
    header_code = None
    if fmt == "A" and di + 2 < len(lines):
        m = re.search(r"Unit Price \(([A-Z]{3})\)", lines[di + 2])
        header_code = m.group(1) if m else None
    items_start = di + (3 if fmt == "A" else 4)
    totals_idx = next(
        (i for i in range(items_start, len(lines)) if lines[i].startswith("Subtotal")),
        None,
    )
    if totals_idx is None:
        raise ExtractionError(f"{source_name}: totals block missing")

    items: list[dict] = []
    items_ok = True
    chunk = lines[items_start:totals_idx]
    if len(chunk) % 4 != 0:
        items_ok = False
    else:
        for i in range(0, len(chunk), 4):
            desc, qty_t, unit_t, amt_t = chunk[i:i + 4]
            qty = None
            if re.match(r"^\d{1,3}(,\d{3})*$", qty_t) or qty_t.isdigit():
                qty = int(qty_t.replace(",", ""))
            unit = parse_money(unit_t)
            amount = parse_money(amt_t)
            if qty is None or unit is None or amount is None:
                items_ok = False
                break
            items.append({
                "description": desc, "qty": qty,
                "unit_price": unit, "amount": amount,
            })
    rec["line_items"] = items
    if not items_ok or not items:
        warnings.append("line_items_parse_suspect")

    # totals — RAW token capture; verification owns the wrap decision.
    # Document reality in this batch: Format A prints "TOTAL" alone, the amount on the NEXT
    # line; AlpinaDiesel prints "TOTAL €4" then "2,121.24"; StationOps/OldGuard/SecureGuard
    # print "TOTAL" / "€10,010.4" / "6". So we collect the money-ish tokens that follow the
    # TOTAL marker, in document order, and let verification decide clean vs wrapped.
    total_line_text = next((ln for ln in lines if ln.startswith("TOTAL")), None)
    prefix_code = None
    tokens: list[str] = []

    if fmt == "A":
        subtotal = parse_money(lines[totals_idx + 1]) if totals_idx + 1 < len(lines) else None
        vat_idx = next((i for i, ln in enumerate(lines) if ln.startswith("VAT (19%")), None)
        vat = parse_money(lines[vat_idx + 1]) if vat_idx is not None else None
        tot_idx = next((i for i, ln in enumerate(lines) if ln.startswith("TOTAL")), None)
        if tot_idx is not None:
            remainder = lines[tot_idx].split("TOTAL", 1)[1].strip()
            if remainder:
                tokens.append(remainder)
            for nxt in lines[tot_idx + 1: tot_idx + 4]:
                if MONEYISH_TOKEN_RE.match(nxt):
                    tokens.append(nxt)
                else:
                    break
    else:  # B: "Subtotal: EUR 1350.00" / "TOTAL: EUR 1606.50" — single-line totals
        def b_value(line: str) -> tuple[Decimal | None, str | None]:
            m = re.match(r"^(?:Subtotal|VAT 19%|TOTAL):\s*(?:([A-Z]{3})\s+)?(.+)$", line)
            if not m:
                return None, None
            return parse_money(m.group(2)), m.group(1)

        subtotal, _ = b_value(lines[totals_idx])
        vat_line = next((ln for ln in lines if ln.startswith("VAT 19%")), None)
        vat, _ = b_value(vat_line) if vat_line else (None, None)
        if total_line_text:
            after = total_line_text.split("TOTAL:", 1)[1].strip()
            if after:
                tokens.append(after)
            m = re.match(r"^([A-Z]{3})\s+", after or "")
            prefix_code = m.group(1) if m else None

    rec["subtotal"] = subtotal
    rec["vat_amount"] = vat
    # the witness reads the total NAIVELY (first token only) — for AlpinaDiesel that is '€4',
    # which is exactly what makes the page-wrap visible. Reconstructing the wrap is
    # verification's job (arithmetic authority), never the witness's.
    rec["total"] = parse_money(tokens[0]) if tokens else None
    rec["total_raw"] = {"line_text": total_line_text, "tokens": tokens}
    if subtotal is None or vat is None or not tokens:
        warnings.append("totals_parse_suspect")

    # currency: three independent signals, agreement is verification's business
    symbols = "".join(tokens)
    symbol_code = next((code for sym, code in SYMBOL_CURRENCY.items() if sym in symbols), None)
    rec["currency_signals"] = {
        "header_code": header_code,
        "symbol": symbol_code,
        "prefix_code": prefix_code,
    }
    present = [v for v in rec["currency_signals"].values() if v]
    rec["currency"] = present[0] if present else None

    # payment details: " | " (A) or double-space (B) separated
    pay_line = next((ln for ln in lines if ln.startswith("IBAN:")), None)
    if pay_line:
        m = re.match(
            r"IBAN:\s*(\S+)\s+BIC:\s*(\S+)\s+Bank:\s*(.+)$",
            pay_line.replace(" | ", "  "),
        )
        rec["payment"] = (
            {"iban": m.group(1), "bic": m.group(2), "bank": m.group(3).strip()}
            if m else {"iban": None, "bic": None, "bank": None, "raw": pay_line}
        )
    else:
        rec["payment"] = {"iban": None, "bic": None, "bank": None}

    note_line = next((ln for ln in lines if ln.startswith("Note:")), None)
    rec["free_text_note"] = note_line.split(":", 1)[1].strip() if note_line else None

    rec["warnings"] = warnings
    rec["extraction_confidence"] = 1.0 if not warnings else 0.5
    return rec
