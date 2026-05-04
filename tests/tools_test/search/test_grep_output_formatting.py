"""Tests for grep_tool output formatting: pagination hints, metadata footer,
and _SearchResult model behavior."""

import pytest

from src.tools.search.grep_tool.grep_tool import (
    _format_output,
    _SearchResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(entries=None, total_matches=0, total_files=0,
                 truncated=False, applied_offset=None, applied_limit=None):
    """Create a _SearchResult with the given parameters."""
    return _SearchResult(
        entries=entries or [],
        total_matches=total_matches,
        total_files=total_files,
        truncated=truncated,
        applied_offset=applied_offset,
        applied_limit=applied_limit,
    )


# ===========================================================================
# Pagination hint: [Use offset=N to see more results.]
# ===========================================================================

class TestPaginationHint:
    """Pagination hint should appear only when truncated AND applied_limit set."""

    def test_truncated_with_limit_shows_hint(self):
        """When truncated=True and applied_limit is set, pagination hint appears."""
        result = _make_result(
            entries=[("file.py", 1, "line 1")],
            total_matches=100,
            total_files=1,
            truncated=True,
            applied_offset=None,
            applied_limit=10,
        )
        output = _format_output(result, "content", 5)
        assert "[Use offset=10 to see more results.]" in output

    def test_truncated_with_offset_calculates_next(self):
        """Next offset = current offset + limit."""
        result = _make_result(
            entries=[("file.py", 1, "line 1")],
            total_matches=100,
            total_files=1,
            truncated=True,
            applied_offset=50,
            applied_limit=10,
        )
        output = _format_output(result, "content", 5)
        assert "[Use offset=60 to see more results.]" in output

    def test_not_truncated_no_hint(self):
        """When truncated=False, no pagination hint."""
        result = _make_result(
            entries=[("file.py", 1, "line 1")],
            total_matches=1,
            total_files=1,
            truncated=False,
            applied_offset=None,
            applied_limit=None,
        )
        output = _format_output(result, "content", 5)
        assert "offset=" not in output

    def test_truncated_but_no_limit_no_hint(self):
        """Edge case: truncated=True but applied_limit=None -> no hint."""
        result = _make_result(
            entries=[("file.py", 1, "line 1")],
            total_matches=5,
            total_files=1,
            truncated=True,
            applied_offset=None,
            applied_limit=None,
        )
        output = _format_output(result, "content", 5)
        assert "Use offset=" not in output

    def test_zero_offset_shows_correct_next(self):
        """offset=0 means hint should say offset=<limit>."""
        result = _make_result(
            entries=[("file.py", 1, "line 1")],
            total_matches=50,
            total_files=1,
            truncated=True,
            applied_offset=0,  # explicit zero
            applied_limit=25,
        )
        output = _format_output(result, "content", 5)
        assert "[Use offset=25 to see more results.]" in output


# ===========================================================================
# Metadata footer content
# ===========================================================================

class TestMetadataFooter:
    """Verify the metadata footer contains correct information."""

    def test_footer_contains_match_count(self):
        result = _make_result(
            entries=[("f.py", 1, "x")],
            total_matches=42,
            total_files=3,
        )
        output = _format_output(result, "content", 10)
        assert "42 matches" in output

    def test_footer_contains_file_count(self):
        result = _make_result(
            entries=[("f.py", 1, "x")],
            total_matches=1,
            total_files=7,
        )
        output = _format_output(result, "content", 10)
        assert "7 files" in output

    def test_footer_contains_duration(self):
        result = _make_result(
            entries=[("f.py", 1, "x")],
            total_matches=1,
            total_files=1,
        )
        output = _format_output(result, "content", 123)
        assert "123ms" in output

    def test_footer_shows_truncated_flag(self):
        result = _make_result(
            entries=[("f.py", 1, "x")],
            total_matches=100,
            total_files=1,
            truncated=True,
            applied_limit=10,
        )
        output = _format_output(result, "content", 5)
        assert "truncated: true" in output

    def test_footer_shows_offset(self):
        result = _make_result(
            entries=[("f.py", 1, "x")],
            total_matches=10,
            total_files=1,
            applied_offset=20,
        )
        output = _format_output(result, "content", 5)
        assert "offset: 20" in output

    def test_footer_shows_limit(self):
        result = _make_result(
            entries=[("f.py", 1, "x")],
            total_matches=100,
            total_files=1,
            truncated=True,
            applied_limit=50,
        )
        output = _format_output(result, "content", 5)
        assert "limit: 50" in output


# ===========================================================================
# Empty results
# ===========================================================================

class TestEmptyResults:
    """No matches should return a clean message."""

    def test_no_entries_returns_no_matches(self):
        result = _make_result()
        output = _format_output(result, "content", 10)
        assert output == "No matches found."

    def test_no_entries_files_mode(self):
        result = _make_result()
        output = _format_output(result, "files_with_matches", 10)
        assert output == "No matches found."


# ===========================================================================
# Output modes
# ===========================================================================

class TestOutputModes:
    """Different output modes format entries differently."""

    def test_content_mode_has_line_numbers(self):
        result = _make_result(
            entries=[("src/main.py", 42, "def hello():")],
            total_matches=1, total_files=1,
        )
        output = _format_output(result, "content", 5)
        assert "42" in output
        assert "def hello():" in output
        assert "# src/main.py" in output

    def test_files_mode_shows_paths_only(self):
        result = _make_result(
            entries=[("a.py", 0, ""), ("b.py", 0, "")],
            total_matches=2, total_files=2,
        )
        output = _format_output(result, "files_with_matches", 5)
        assert "a.py" in output
        assert "b.py" in output

    def test_count_mode_shows_counts(self):
        result = _make_result(
            entries=[("a.py", 5, ""), ("b.py", 3, "")],
            total_matches=8, total_files=2,
        )
        output = _format_output(result, "count", 5)
        assert "a.py: 5" in output
        assert "b.py: 3" in output
