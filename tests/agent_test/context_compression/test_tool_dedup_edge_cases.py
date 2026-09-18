"""Tests for canonical context-compression tool-name constants."""

from agentloom.adapters.smolagents.context_compression import (
    FILE_READ_TOOL_NAMES,
    TOOL_MAX_RETAIN_CHARS,
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
# FILE_READ_TOOL_NAMES
# ===========================================================================

class TestFileReadToolNames:
    """Verify the canonical file-read Tool set."""

    def test_is_frozenset(self):
        assert isinstance(FILE_READ_TOOL_NAMES, frozenset)

    def test_contains_only_canonical_read_tools(self):
        assert FILE_READ_TOOL_NAMES == frozenset(
            {"read_file", "get_file_outline"}
        )

    def test_contains_read_file(self):
        assert "read_file" in FILE_READ_TOOL_NAMES

    def test_does_not_contain_search_tools(self):
        assert "grep_search" not in FILE_READ_TOOL_NAMES
        assert "glob_search" not in FILE_READ_TOOL_NAMES
