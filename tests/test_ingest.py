"""Ingest / control record tests (S2). Ties the reviewer to the assignment's batch size."""
from src.ingest import ingest


def test_control_record_counts_and_uniqueness(invoices_dir):
    control = ingest(invoices_dir, expected=17)
    assert control.found == 17
    assert control.ok is True
    assert control.duplicate_hashes == []
    assert all(len(f.sha256) == 64 for f in control.files)


def test_control_record_detects_wrong_expected_count(invoices_dir):
    control = ingest(invoices_dir, expected=16)
    assert control.ok is False


def test_control_record_is_deterministically_sorted(invoices_dir):
    names = [f.name for f in ingest(invoices_dir, expected=17).files]
    assert names == sorted(names)
