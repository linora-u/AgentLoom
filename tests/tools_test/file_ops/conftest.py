from pathlib import Path
import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[2]

@pytest.fixture(scope="session")
def test_dir():
    """Provides a temporary test directory path."""
    return AGENT_LOOM_ROOT / "temp_test_file_editor"
