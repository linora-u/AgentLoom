"""Tests for registry-backed built-in tool resolution."""

import importlib

import pytest

from src.lib.config import C
from src.tools import DEFAULT_TOOLSETS
from src.tools.tool_meta import (
    get_tool_spec,
    list_tool_specs,
    list_toolsets,
    resolve_tool_function,
    resolve_toolsets,
)


class TestDefaultToolsetsResolve:
    def test_system_default_toolsets_are_valid(self):
        raw_toolsets = C.get("default_toolsets", [])
        assert raw_toolsets == list(DEFAULT_TOOLSETS)
        assert resolve_toolsets(raw_toolsets) == [
            "shell_tool",
            "check_background_task",
            "kill_background_task",
            "list_background_tasks",
            "read_file",
            "edit_file",
            "write_file",
            "list_directory",
            "grep_search",
            "glob_search",
            "loom_retrieve_context",
            "load_skill",
            "list_skills",
        ]

    def test_all_default_tools_resolve(self):
        failures = []
        for tool_name in resolve_toolsets(C.get("default_toolsets", [])):
            try:
                func = resolve_tool_function(tool_name)
                if not callable(func):
                    failures.append(f"{tool_name}: resolved but not callable")
            except ValueError as exc:
                failures.append(f"{tool_name}: {exc}")
        assert not failures

    @pytest.mark.parametrize("tool_name", resolve_toolsets(DEFAULT_TOOLSETS))
    def test_individual_default_tool_resolves(self, tool_name):
        assert callable(resolve_tool_function(tool_name))


class TestToolsets:
    def test_known_toolsets(self):
        toolsets = list_toolsets()
        assert set(DEFAULT_TOOLSETS).issubset(toolsets)
        assert toolsets["markdown_report"] == (
            "write_markdown_file",
            "write_markdown_file_raw",
            "append_markdown_sections",
        )
        assert "get_file_outline" in toolsets["code_nav"]

    def test_empty_toolsets_means_no_builtins(self):
        assert resolve_toolsets([]) == []

    def test_unknown_toolset_fails(self):
        with pytest.raises(ValueError, match="Unknown toolset"):
            resolve_toolsets(["legacy"])


class TestDynamicModuleLoading:
    def test_grep_search_from_search_module(self):
        mod = importlib.import_module("src.tools.search")
        assert callable(mod.grep_search)

    def test_read_file_from_file_ops_module(self):
        mod = importlib.import_module("src.tools.file_ops")
        assert callable(mod.read_file)

    def test_resolved_function_matches_module_function(self):
        from src.tools.search import grep_search as direct

        assert resolve_tool_function("grep_search") is direct


class TestAdditionalToolsetsResolve:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "write_markdown_file",
            "write_markdown_file_raw",
            "append_markdown_sections",
            "get_file_outline",
            "ast_grep_search_file",
            "lsp_find_definition",
            "lsp_find_references",
            "lsp_get_document_symbols",
            "lsp_hover",
            "lsp_get_workspace_symbols",
        ],
    )
    def test_optional_registry_tool_resolves(self, tool_name):
        assert callable(resolve_tool_function(tool_name))


class TestRemovedToolsRaise:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "browse_directory",
            "code_search",
            "code_replace",
            "code_edit",
            "search_and_replace",
            "write_whole_file",
            "delete_file",
            "move_file",
            "rename_file",
            "copy_file",
            "search_files",
            "get_git_diff_content",
            "git_grep_files",
            "is_path_in_repo",
            "git_commit_files",
            "git_auto_commit",
            "git_check_dirty",
        ],
    )
    def test_removed_tool_names_do_not_resolve(self, tool_name):
        with pytest.raises(ValueError, match="not a registered built-in tool"):
            resolve_tool_function(tool_name)


class TestRegistryInvariants:
    def test_tool_names_are_unique(self):
        names = [spec.name for spec in list_tool_specs()]
        assert len(names) == len(set(names))

    def test_all_specs_match_function_names(self):
        for spec in list_tool_specs():
            assert get_tool_spec(spec.name) is spec
            assert callable(spec.function)

    def test_case_sensitive(self):
        with pytest.raises(ValueError):
            resolve_tool_function("Grep_Search")
