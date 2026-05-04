"""Tests for context_compression constants after tool alias rename.

Verifies that TOOL_MAX_RETAIN_CHARS and TOOL_DEDUP_PATTERNS use the current
canonical tool names (glob_search, grep_search) and that old alias names
(list_files_glob, ripgrep_search_directory) are no longer present.
"""

import pytest

from src.lib.smolagents.memory.context_compression import (
    TOOL_MAX_RETAIN_CHARS,
    TOOL_DEDUP_PATTERNS,
    FILE_READ_TOOL_NAMES,
)


# ===========================================================================
# TOOL_MAX_RETAIN_CHARS — alias rename verification
# ===========================================================================

class TestToolMaxRetainCharsAliasRename:
    """Verify current tool names are used, old aliases are removed."""

    def test_grep_search_key_exists(self):
        assert "grep_search" in TOOL_MAX_RETAIN_CHARS

    def test_glob_search_key_exists(self):
        assert "glob_search" in TOOL_MAX_RETAIN_CHARS

    def test_old_ripgrep_alias_removed(self):
        """Old alias 'ripgrep_search_directory' should NOT be in the dict."""
        assert "ripgrep_search_directory" not in TOOL_MAX_RETAIN_CHARS

    def test_old_list_files_glob_alias_removed(self):
        """Old alias 'list_files_glob' should NOT be in the dict."""
        assert "list_files_glob" not in TOOL_MAX_RETAIN_CHARS

    def test_default_key_exists(self):
        assert "default" in TOOL_MAX_RETAIN_CHARS

    def test_shell_tool_key_exists(self):
        assert "shell_tool" in TOOL_MAX_RETAIN_CHARS

    def test_read_file_exempt(self):
        """read_file should be exempt (None) — handled by dedup layer."""
        assert TOOL_MAX_RETAIN_CHARS.get("read_file") is None

    def test_grep_search_has_positive_limit(self):
        limit = TOOL_MAX_RETAIN_CHARS["grep_search"]
        assert isinstance(limit, int) and limit > 0

    def test_glob_search_has_positive_limit(self):
        limit = TOOL_MAX_RETAIN_CHARS["glob_search"]
        assert isinstance(limit, int) and limit > 0

    def test_unknown_tool_uses_default(self):
        """Tools not in the dict should use the 'default' fallback."""
        assert "some_unknown_tool" not in TOOL_MAX_RETAIN_CHARS
        assert TOOL_MAX_RETAIN_CHARS["default"] > 0


# ===========================================================================
# TOOL_DEDUP_PATTERNS — file path extraction
# ===========================================================================

class TestToolDedupPatterns:
    """Verify dedup patterns work correctly for file-read tools."""

    def test_read_file_pattern_exists(self):
        assert "read_file" in TOOL_DEDUP_PATTERNS

    def test_read_file_pattern_exists(self):
        assert "read_file" in TOOL_DEDUP_PATTERNS

    def test_get_file_outline_pattern_exists(self):
        assert "get_file_outline" in TOOL_DEDUP_PATTERNS

    def test_read_file_extracts_path(self):
        """Pattern should extract file path from a tool call string."""
        regex = TOOL_DEDUP_PATTERNS["read_file"]
        text = 'read_file("src/main.py")'
        match = regex.search(text)
        assert match is not None
        assert match.group(1) == "src/main.py"

    def test_read_file_extracts_single_quotes(self):
        regex = TOOL_DEDUP_PATTERNS["read_file"]
        text = "read_file('config/system.yaml')"
        match = regex.search(text)
        assert match is not None
        assert match.group(1) == "config/system.yaml"

    def test_read_file_extracts_path(self):
        regex = TOOL_DEDUP_PATTERNS["read_file"]
        text = 'read_file("src/utils.py")'
        match = regex.search(text)
        assert match is not None
        assert match.group(1) == "src/utils.py"

    def test_unrecognized_tool_not_in_patterns(self):
        """Tools like grep_search should NOT have dedup patterns."""
        assert "grep_search" not in TOOL_DEDUP_PATTERNS
        assert "glob_search" not in TOOL_DEDUP_PATTERNS
        assert "shell_tool" not in TOOL_DEDUP_PATTERNS

    def test_no_match_on_garbage_input(self):
        """Pattern should not match invalid call syntax."""
        regex = TOOL_DEDUP_PATTERNS["read_file"]
        assert regex.search("read_file()") is None
        assert regex.search("read_file") is None

    def test_path_with_spaces(self):
        """File paths containing spaces should be extracted."""
        regex = TOOL_DEDUP_PATTERNS["read_file"]
        text = 'read_file("path with spaces/file.py")'
        match = regex.search(text)
        assert match is not None
        assert match.group(1) == "path with spaces/file.py"


# ===========================================================================
# FILE_READ_TOOL_NAMES
# ===========================================================================

class TestFileReadToolNames:
    """Verify FILE_READ_TOOL_NAMES is consistent with TOOL_DEDUP_PATTERNS."""

    def test_is_frozenset(self):
        assert isinstance(FILE_READ_TOOL_NAMES, frozenset)

    def test_matches_dedup_pattern_keys(self):
        """FILE_READ_TOOL_NAMES should contain exactly the TOOL_DEDUP_PATTERNS keys."""
        assert FILE_READ_TOOL_NAMES == frozenset(TOOL_DEDUP_PATTERNS.keys())

    def test_contains_read_file(self):
        assert "read_file" in FILE_READ_TOOL_NAMES

    def test_does_not_contain_search_tools(self):
        assert "grep_search" not in FILE_READ_TOOL_NAMES
        assert "glob_search" not in FILE_READ_TOOL_NAMES
