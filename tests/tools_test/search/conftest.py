from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent

GREP_TESTDATA_ROOT = SCRIPT_DIR / "testdata"


@pytest.fixture(scope="session")
def grep_testdata_path() -> str:
    return str(GREP_TESTDATA_ROOT)
