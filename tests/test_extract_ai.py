"""AI-primary extraction smoke test — needs a running local Ollama (marker: llm).

Deselect with:  pytest -m "not llm"
"""
import pytest

from src.extract_ai import extract_invoice_ai
from src.extract_regex import read_pdf_text
from src.llm_provider import is_reachable, unload
from src.validate_extract import verify

BASE_URL = "http://127.0.0.1:11434"
MODEL = "llama3.2:3b"

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(not is_reachable(BASE_URL), reason="Ollama not running on 127.0.0.1:11434"),
]


@pytest.fixture(scope="module", autouse=True)
def _free_model_after_tests():
    """Directive G9: never leave the model resident in RAM after computation stops."""
    yield
    unload(MODEL, BASE_URL)


def test_ai_primary_extraction_on_eurofuel(invoices_dir):
    path = invoices_dir / "INV-2025-03701_V001_EuroFuel.pdf"
    rec = extract_invoice_ai(read_pdf_text(path), path.name, model=MODEL, base_url=BASE_URL)

    assert rec["extraction_layer"] == "ai_primary"
    assert rec["invoice_number"] == "INV-2025-03701"
    assert rec["invoice_date"] == "2025-06-05"
    assert rec["po_number"] == "PO-55210"
    assert rec["currency"] == "EUR"
    assert rec["vendor_name"] == "EuroFuel GmbH"
    assert len(rec["line_items"]) == 3
    assert rec["model"] == MODEL and rec["prompt_version"]
    assert rec["latency_ms"] > 0

    verify(rec)
    assert rec["verification"]["total_agrees_with_ai"] is True
    assert rec["verification"]["line_math_ok"] is True
    assert rec["total_trusted"] == rec["total"]
