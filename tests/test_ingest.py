"""Ingest / control record tests (S2). Ties the reviewer to the assignment's batch size.

The CLI-level tests drive run.main fully offline over staged temp data dirs (the
test_review.py pattern): the shipped config keeps auto-detect, a monkeypatched load_config
pins strict, and a foreign oracle fixture scopes the §5.3 check to N/A for a custom dataset.
"""
import shutil

import run
from src import summarize
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


def _copy_distinct_pdfs(source_dir, target_dir, count):
    """Copies the first `count` committed PDFs (distinct bytes) into target_dir."""
    pdfs = sorted(source_dir.glob("*.pdf"), key=lambda p: p.name)[:count]
    for pdf in pdfs:
        shutil.copy2(pdf, target_dir / pdf.name)
    return pdfs


def test_auto_detect_accepts_a_batch_of_any_size(tmp_path, invoices_dir):
    pdfs = _copy_distinct_pdfs(invoices_dir, tmp_path, 5)
    control = ingest(tmp_path, None)
    assert control.found == len(pdfs) == control.expected == 5
    assert control.ok is True
    assert control.mode == "auto-detect"
    assert control.duplicate_hashes == []
    assert ingest(tmp_path, 0).mode == "auto-detect"


def test_strict_mode_still_fails_on_count_mismatch(tmp_path, invoices_dir):
    _copy_distinct_pdfs(invoices_dir, tmp_path, 5)
    control = ingest(tmp_path, 6)
    assert control.found == 5 and control.expected == 6
    assert control.ok is False
    assert control.mode == "strict"
    assert control.duplicate_hashes == []


def test_duplicate_hashes_reject_in_both_modes(tmp_path, invoices_dir):
    pdf = sorted(invoices_dir.glob("*.pdf"))[0]
    shutil.copy2(pdf, tmp_path / pdf.name)
    shutil.copy2(pdf, tmp_path / "same-bytes-copy.pdf")
    auto = ingest(tmp_path, None)
    strict = ingest(tmp_path, 2)
    assert auto.duplicate_hashes and auto.mode == "auto-detect"
    assert strict.duplicate_hashes and strict.mode == "strict"


def test_committed_batch_pinned_at_17_still_passes(invoices_dir):
    control = ingest(invoices_dir, 17)
    assert control.found == control.expected == 17
    assert control.ok is True
    assert control.mode == "strict"
    assert control.duplicate_hashes == []


# --------------------------------------------------------------------- run.main CLI (S2 modes)

def _stage_data_dir(source_dir, tmp_path, pdf_count):
    """A run-able data dir under tmp_path: pdf_count distinct PDFs + the two committed CSVs."""
    invoices = tmp_path / "invoices"
    invoices.mkdir()
    _copy_distinct_pdfs(source_dir / "invoices", invoices, pdf_count)
    for name in ("vendor_master.csv", "historic_invoices.csv"):
        shutil.copy2(source_dir / name, tmp_path / name)
    return tmp_path


def _foreign_oracle(_path=None):
    """An oracle fixture whose invoice keys share nothing with the committed assessment set."""
    return {"source": "custom dataset (test fixture)",
            "totals": {"CLEAR": 3, "FLAG-REVIEW": 0, "HOLD": 0},
            "invoices": {f"INV-CUSTOM-{i:04d}": {"expected": "CLEAR"} for i in range(1, 4)}}


def test_run_main_auto_detect_runs_a_small_batch_and_scopes_oracle_to_na(
        data_dir, tmp_path, monkeypatch, capsys):
    staged = _stage_data_dir(data_dir, tmp_path, 3)
    monkeypatch.setattr(summarize, "load_oracle", _foreign_oracle)
    rc = run.main(["--no-ai", "--data-dir", str(staged), "--out-dir", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "auto-detect (no truncation control)" in out
    assert ("Oracle Status    : N/A — custom dataset "
            "(no overlap with the §5.3 assessment set)") in out
    assert "Not processed:" not in out and "Mismatch ->" not in out


def test_run_main_strict_count_mismatch_aborts_before_extraction(
        data_dir, tmp_path, monkeypatch, capsys):
    staged = _stage_data_dir(data_dir, tmp_path, 3)
    fake_config = {**run.load_config(), "expected_invoice_count": 99}
    monkeypatch.setattr(run, "load_config", lambda: fake_config)
    rc = run.main(["--no-ai", "--data-dir", str(staged), "--out-dir", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "strict (expected_invoice_count=99)" in out
    assert "[ 1/" not in out


def test_run_main_rejects_duplicate_pdfs_in_both_modes(data_dir, tmp_path, monkeypatch, capsys):
    staged = _stage_data_dir(data_dir, tmp_path, 1)
    pdf = sorted((staged / "invoices").glob("*.pdf"))[0]
    shutil.copy2(pdf, staged / "invoices" / "same-bytes-copy.pdf")

    rc_auto = run.main(["--no-ai", "--data-dir", str(staged),
                        "--out-dir", str(staged / "out-auto")])
    auto_out = capsys.readouterr().out
    assert rc_auto == 1
    assert "auto-detect (no truncation control)" in auto_out
    assert "NO — DUPLICATES PRESENT" in auto_out

    fake_config = {**run.load_config(), "expected_invoice_count": 2}
    monkeypatch.setattr(run, "load_config", lambda: fake_config)
    rc_strict = run.main(["--no-ai", "--data-dir", str(staged),
                          "--out-dir", str(staged / "out-strict")])
    strict_out = capsys.readouterr().out
    assert rc_strict == 1
    assert "strict (expected_invoice_count=2)" in strict_out
    assert "NO — DUPLICATES PRESENT" in strict_out


def test_run_main_empty_invoices_dir_returns_nothing_to_process(data_dir, tmp_path, capsys):
    staged = _stage_data_dir(data_dir, tmp_path, 0)
    rc = run.main(["--no-ai", "--data-dir", str(staged), "--out-dir", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert rc == 2
    assert f"no PDFs found in {staged / 'invoices'}" in out
