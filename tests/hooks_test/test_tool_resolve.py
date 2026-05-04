"""Tests for convention-based tool resolution end-to-end.

Verifies that all tools listed in config/system.yaml tools.default resolve
successfully, that dynamic module loading still works, and that obsolete
alias names correctly raise errors.
"""

import importlib
import pytest

from src.tools.tool_meta import resolve_tool_function
from src.lib.config import C


# ---------------------------------------------------------------------------
# All tools in config/system.yaml tools.default resolve successfully
# ---------------------------------------------------------------------------

class TestAllDefaultToolsResolve:
    """Every tool in the system.yaml default tools list must be resolvable."""

    @staticmethod
    def _get_default_tools():
        """Read the default_loaded_tools list from system config."""
        default_list = C.get("default_loaded_tools", [])
        assert isinstance(default_list, list), "default_loaded_tools must be a list"
        assert len(default_list) > 0, "default_loaded_tools must not be empty"
        return default_list

    def test_all_default_tools_resolve(self):
        """Each tool in tools.default should resolve to a callable."""
        default_tools = self._get_default_tools()
        failures = []
        for tool_name in default_tools:
            try:
                func = resolve_tool_function(tool_name)
                if not callable(func):
                    failures.append(f"{tool_name}: resolved but not callable")
            except ValueError as e:
                failures.append(f"{tool_name}: {e}")
        assert not failures, f"Failed to resolve tools:\n" + "\n".join(failures)

    @pytest.mark.parametrize("tool_name", [
        "load_skill",
        "list_skills",
        "shell_tool",
        "read_file",
        "grep_search",
        "glob_search",
        "lsp_find_definition",
        "lsp_find_references",
        "lsp_get_document_symbols",
        "edit_file",
        "write_markdown_file",
    ])
    def test_individual_default_tool_resolves(self, tool_name):
        """Parametrized: each known default tool resolves."""
        func = resolve_tool_function(tool_name)
        assert callable(func)


# ---------------------------------------------------------------------------
# Dynamic module+function loading
# ---------------------------------------------------------------------------

class TestDynamicModuleLoading:
    """The tools package re-exports functions from sub-modules; verify."""

    def test_grep_search_from_search_module(self):
        """grep_search originates from src.tools.search module."""
        mod = importlib.import_module("src.tools.search")
        assert hasattr(mod, "grep_search")
        assert callable(mod.grep_search)

    def test_read_file_from_file_ops_module(self):
        """read_file originates from src.tools.file_ops module."""
        mod = importlib.import_module("src.tools.file_ops")
        assert hasattr(mod, "read_file")
        assert callable(mod.read_file)

    def test_shell_tool_from_shell_module(self):
        """shell_tool originates from src.tools.shell module."""
        mod = importlib.import_module("src.tools.shell")
        assert hasattr(mod, "shell_tool")
        assert callable(mod.shell_tool)

    def test_resolved_function_matches_module_function(self):
        """resolve_tool_function should return the same object as direct import."""
        from src.tools.search import grep_search as direct
        resolved = resolve_tool_function("grep_search")
        assert resolved is direct

    def test_tools_package_exports_all_defaults(self):
        """The __all__ list in src.tools should contain all defaults."""
        import src.tools as tools_pkg
        all_names = getattr(tools_pkg, "__all__", [])
        for tool_name in ["grep_search", "glob_search", "shell_tool",
                          "read_file", "edit_file"]:
            assert tool_name in all_names, f"{tool_name} missing from __all__"


# ---------------------------------------------------------------------------
# Additional known tools resolve (beyond default list)
# ---------------------------------------------------------------------------

class TestAdditionalToolsResolve:
    """Tools not in the default list but available in the package."""

    @pytest.mark.parametrize("tool_name", [
        "write_file",
        "read_file",
        "edit_file",
        "get_file_outline",
        "browse_directory",
        "write_markdown_file_raw",
        "append_markdown_sections",
        "delete_file",
        "move_file",
        "rename_file",
        "copy_file",
        "search_files",
        "code_search",
        "code_replace",
        "code_edit",
        "search_and_replace",
        "write_whole_file",
        "get_git_diff_content",
        "git_grep_files",
        "is_path_in_repo",
        "ast_grep_search_file",
    ])
    def test_extra_tool_resolves(self, tool_name):
        func = resolve_tool_function(tool_name)
        assert callable(func)


# ---------------------------------------------------------------------------
# Obsolete / alias names should NOT resolve
# ---------------------------------------------------------------------------

class TestObsoleteAliasesRaise:
    """Old alias names that were removed/renamed should raise ValueError."""

    @pytest.mark.parametrize("alias_name", [
        "search_keyword_in_directory",
        "find_files_by_pattern",
        "execute_bash",
        "run_command",
    ])
    def test_old_alias_raises_valueerror(self, alias_name):
        with pytest.raises(ValueError, match="not found"):
            resolve_tool_function(alias_name)


# ---------------------------------------------------------------------------
# Boundary: special characters and edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for tool resolution."""

    def test_whitespace_name_raises(self):
        with pytest.raises(ValueError):
            resolve_tool_function("  ")

    def test_none_like_string_raises(self):
        with pytest.raises(ValueError):
            resolve_tool_function("None")

    def test_module_path_not_a_tool(self):
        """Dotted module paths should not resolve via convention."""
        with pytest.raises(ValueError):
            resolve_tool_function("src.tools.search.grep_search")

    def test_case_sensitive(self):
        """Tool names are case-sensitive; wrong case should fail."""
        with pytest.raises(ValueError):
            resolve_tool_function("Grep_Search")
        with pytest.raises(ValueError):
            resolve_tool_function("GREP_SEARCH")
