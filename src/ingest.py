"""S2 — Ingest: hash, land, control record. Assets are consumed here and only here.

Deterministic by nature (I/O, not intelligence). The control record is the pipeline's
first fail-safe (P-E01): wrong file count aborts the run before any AI call is made.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InvoiceFile:
    """Immutable representation of an ingested PDF file and its cryptographic hash."""
    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ControlRecord:
    """Batch ingestion status record containing audit checks on file count and duplicates."""
    invoices_dir: str
    expected: int
    found: int
    files: tuple[InvoiceFile, ...]

    @property
    def ok(self) -> bool:
        """Returns True if the number of discovered files matches the expected count."""
        return self.found == self.expected

    @property
    def duplicate_hashes(self) -> list[str]:
        """Identifies any SHA-256 collision / duplicate uploaded PDFs within the batch."""
        seen: dict[str, int] = {}
        for f in self.files:
            seen[f.sha256] = seen.get(f.sha256, 0) + 1
        return [h for h, n in seen.items() if n > 1]


def sha256_of(path: Path) -> str:
    """Calculates SHA-256 hash of a file on disk in 64KB streaming chunks.

    Args:
        path: Path to the target file.

    Returns:
        Hexadecimal SHA-256 digest string.

    Where called:
        Called by ingest() for each PDF file in the input directory.

    Where it sends information:
        Returned to ingest() to populate InvoiceFile.sha256 for audit logging.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def ingest(invoices_dir: Path, expected: int) -> ControlRecord:
    """Discovers all PDF invoices in the input folder and constructs the control record gate.

    Args:
        invoices_dir: Directory containing input invoice PDFs.
        expected: Expected number of PDFs from pipeline config.

    Returns:
        ControlRecord instance containing file metadata and validation flags.

    Where called:
        Called by run.py (main execution loop) at Step S2 and by tests/test_ingest.py.

    Where it sends information:
        Returns ControlRecord to run.py. If control.ok is False or duplicate_hashes exist,
        run.py aborts immediately with exit code 1 before any model or extraction runs.
    """
    if not invoices_dir.is_dir():
        raise FileNotFoundError(f"invoices directory not found: {invoices_dir}")
    pdfs = sorted(invoices_dir.glob("*.pdf"), key=lambda p: p.name)
    files = tuple(InvoiceFile(p.name, sha256_of(p), p.stat().st_size) for p in pdfs)
    return ControlRecord(str(invoices_dir), expected, len(files), files)
