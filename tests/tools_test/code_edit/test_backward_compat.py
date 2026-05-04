"""Tests for backward compatibility of renamed tools."""

import pytest

from src.tools import resolve_tool_function


class TestBackwardCompat:
    """Ensure tool names resolve via convention-based lookup."""

    def test_search_and_replace_still_works(self, sample_class_cpp):
        """The old search_and_replace function should still work."""
        from src.tools.code_editor import search_and_replace

        diff = (
            "<<<<<<< SEARCH\n"
            "    Calculator() : result_(0.0), history_count_(0) {}\n"
            "=======\n"
            "    Calculator() : result_(0.0), history_count_(0), compat_(true) {}\n"
            ">>>>>>> REPLACE\n"
        )
        result = search_and_replace(sample_class_cpp, diff)
        assert "Successfully applied" in result

        with open(sample_class_cpp) as f:
            content = f.read()
        assert "compat_(true)" in content

    def test_resolve_tool_names(self):
        """Tool names resolve via convention-based resolution."""
        assert resolve_tool_function("search_and_replace") is not None
        assert resolve_tool_function("write_whole_file") is not None
        assert resolve_tool_function("delete_file") is not None
        assert resolve_tool_function("git_commit_files") is not None
        assert resolve_tool_function("git_auto_commit") is not None
        assert resolve_tool_function("git_check_dirty") is not None
        assert resolve_tool_function("code_search") is not None
        assert resolve_tool_function("code_replace") is not None
        assert resolve_tool_function("code_edit") is not None

    def test_search_and_replace_delegates_to_code_edit(self):
        """search_and_replace should be the same as code_edit."""
        from src.tools.code_editor import search_and_replace
        from src.tools.code_editor.code_edit_tool import code_edit

        # search_and_replace is a direct alias for code_edit
        assert search_and_replace is code_edit
