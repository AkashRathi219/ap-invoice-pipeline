"""Shared fixtures. Cross-platform by construction: paths resolve relative to this repo (G2)."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT.parent / "ap-automation-assessment"


def _require_data_dir() -> Path:
    if not (DATA_DIR / "invoices").is_dir():
        pytest.skip(
            f"assessment data not found at {DATA_DIR} — clone the starter pack beside this repo "
            "or set AP_DATA_DIR / pass --data-dir to run.py"
        )
    return DATA_DIR


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _require_data_dir()


@pytest.fixture(scope="session")
def invoices_dir(data_dir: Path) -> Path:
    return data_dir / "invoices"
