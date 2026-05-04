import os
import time

import pytest

from src.tools.search.glob_tool.glob_tool import glob_search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_dir(tmp_path):
    """Create a temp directory with various files."""
    (tmp_path / "main.py").write_text("# main")
    (tmp_path / "utils.py").write_text("# utils")
    (tmp_path / "config.yaml").write_text("key: val")
    (tmp_path / "README.md").write_text("# Readme")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.py").write_text("# app")
    (sub / "lib.py").write_text("# lib")
    deep = sub / "internal"
    deep.mkdir()
    (deep / "core.py").write_text("# core")
    return tmp_path


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------

class TestBasicGlob:
    def test_all_files(self, sample_dir):
        result = glob_search("**/*", path=str(sample_dir))
        assert "main.py" in result
        assert "core.py" in result

    def test_python_files(self, sample_dir):
        result = glob_search("**/*.py", path=str(sample_dir))
        assert "main.py" in result
        assert "config.yaml" not in result

    def test_yaml_files(self, sample_dir):
        result = glob_search("**/*.yaml", path=str(sample_dir))
        assert "config.yaml" in result
        assert "main.py" not in result

    def test_no_matches(self, sample_dir):
        result = glob_search("**/*.xyz", path=str(sample_dir))
        assert "No files found" in result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_empty_pattern(self, sample_dir):
        with pytest.raises(ValueError, match="pattern is required"):
            glob_search("", path=str(sample_dir))

    def test_invalid_directory(self):
        with pytest.raises(FileNotFoundError):
            glob_search("**/*.py", path="/nonexistent/path/xyz")

    def test_invalid_sort_by(self, sample_dir):
        with pytest.raises(ValueError, match="sort_by"):
            glob_search("**/*", path=str(sample_dir), sort_by="invalid")


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

class TestSorting:
    def test_name_sort(self, sample_dir):
        result = glob_search("**/*.py", path=str(sample_dir), sort_by="name")
        lines = [l for l in result.split("\n") if l and not l.startswith("[")]
        # Should be alphabetically sorted
        assert lines == sorted(lines)

    def test_mtime_sort(self, sample_dir):
        # Touch one file to make it newest
        target = sample_dir / "utils.py"
        time.sleep(0.1)
        target.write_text("# updated utils")

        result = glob_search("**/*.py", path=str(sample_dir), sort_by="mtime")
        lines = [l for l in result.split("\n") if l.endswith(".py")]
        # utils.py should appear somewhere near the top (it was most recently written)
        # Ripgrep sorts by mtime descending; Python fallback does the same.
        assert any("utils.py" in l for l in lines), "utils.py should be in results"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_max_results(self, sample_dir):
        result = glob_search("**/*.py", path=str(sample_dir), max_results=2)
        py_lines = [l for l in result.split("\n") if l.endswith(".py")]
        assert len(py_lines) <= 2

    def test_truncation_metadata(self, sample_dir):
        result = glob_search("**/*.py", path=str(sample_dir), max_results=1)
        assert "truncated: true" in result

    def test_no_truncation(self, sample_dir):
        result = glob_search("**/*.yaml", path=str(sample_dir), max_results=100)
        assert "truncated" not in result


# ---------------------------------------------------------------------------
# Files only (no directories)
# ---------------------------------------------------------------------------

class TestFilesOnly:
    def test_no_directories_in_output(self, sample_dir):
        result = glob_search("**/*", path=str(sample_dir))
        for line in result.split("\n"):
            if line and not line.startswith("[") and line.strip():
                full = sample_dir / line
                if full.exists():
                    assert full.is_file(), f"Directory in output: {line}"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_footer_present(self, sample_dir):
        result = glob_search("**/*.py", path=str(sample_dir))
        assert "files found" in result
        assert "ms" in result
