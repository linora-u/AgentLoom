"""
Fixtures for code_editor tests.

Provides paths to real C++ test fixture files and helpers
for creating temporary working copies.
"""

import shutil
from pathlib import Path

import pytest

# Root of the testdata directory (real C++ files, permanently stored)
TESTDATA_DIR = Path(__file__).parent / "testdata"


@pytest.fixture(scope="session")
def testdata_dir():
    """Path to the testdata directory containing real C++ fixtures."""
    assert TESTDATA_DIR.exists(), f"testdata dir missing: {TESTDATA_DIR}"
    return TESTDATA_DIR


@pytest.fixture
def sample_class_cpp(tmp_path, testdata_dir):
    """Copy sample_class.cpp to a temporary directory for safe editing."""
    src = testdata_dir / "sample_class.cpp"
    dst = tmp_path / "sample_class.cpp"
    shutil.copy2(src, dst)
    return str(dst)


@pytest.fixture
def sample_algorithm_cpp(tmp_path, testdata_dir):
    """Copy sample_algorithm.cpp to a temporary directory for safe editing."""
    src = testdata_dir / "sample_algorithm.cpp"
    dst = tmp_path / "sample_algorithm.cpp"
    shutil.copy2(src, dst)
    return str(dst)


@pytest.fixture
def sample_template_cpp(tmp_path, testdata_dir):
    """Copy sample_template.cpp to a temporary directory for safe editing."""
    src = testdata_dir / "sample_template.cpp"
    dst = tmp_path / "sample_template.cpp"
    shutil.copy2(src, dst)
    return str(dst)
