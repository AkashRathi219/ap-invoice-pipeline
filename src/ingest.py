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
    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ControlRecord:
    invoices_dir: str
    expected: int
    found: int
    files: tuple[InvoiceFile, ...]

    @property
    def ok(self) -> bool:
        return self.found == self.expected

    @property
    def duplicate_hashes(self) -> list[str]:
        seen: dict[str, int] = {}
        for f in self.files:
            seen[f.sha256] = seen.get(f.sha256, 0) + 1
        return [h for h, n in seen.items() if n > 1]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def ingest(invoices_dir: Path, expected: int) -> ControlRecord:
    if not invoices_dir.is_dir():
        raise FileNotFoundError(f"invoices directory not found: {invoices_dir}")
    pdfs = sorted(invoices_dir.glob("*.pdf"), key=lambda p: p.name)
    files = tuple(InvoiceFile(p.name, sha256_of(p), p.stat().st_size) for p in pdfs)
    return ControlRecord(str(invoices_dir), expected, len(files), files)
