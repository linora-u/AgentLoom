"""Tests for ToolSpec metadata and registry lookup."""

from dataclasses import FrozenInstanceError

import pytest

from src.tools.tool_meta import ToolSpec, get_tool_meta, resolve_tool_function


class TestResolveToolFunction:
    @pytest.mark.parametrize(
        "tool_name",
        ["grep_search", "glob_search", "read_file", "edit_file", "write_file", "shell_tool"],
    )
    def test_registered_tool_resolves(self, tool_name):
        assert callable(resolve_tool_function(tool_name))

    @pytest.mark.parametrize("tool_name", ["nonexistent_tool_xyz", "_private_helper", ""])
    def test_unregistered_tool_raises(self, tool_name):
        with pytest.raises(ValueError, match="registered built-in tool"):
            resolve_tool_function(tool_name)


class TestGetToolMeta:
    def test_returns_toolspec_instance(self):
        meta = get_tool_meta("grep_search")
        assert isinstance(meta, ToolSpec)

    def test_core_search_metadata(self):
        meta = get_tool_meta("grep_search")
        assert meta.name == "grep_search"
        assert meta.toolset == "core_search"
        assert meta.category == "search"
        assert meta.is_read_only is True
        assert meta.output_kind == "search"
        assert meta.max_result_chars == 20000

    def test_shell_tool_metadata(self):
        meta = get_tool_meta("shell_tool")
        assert meta.toolset == "core_shell"
        assert meta.is_concurrency_safe is False
        assert meta.output_kind == "log"

    def test_file_tool_path_params(self):
        assert get_tool_meta("read_file").path_params == ("file_path",)
        assert get_tool_meta("edit_file").path_params == ("file_path",)
        assert get_tool_meta("write_file").path_params == ("file_path",)
        assert get_tool_meta("list_directory").path_params == ("directory_path",)

    def test_unknown_tool_does_not_get_default_metadata(self):
        with pytest.raises(ValueError, match="registered built-in tool"):
            get_tool_meta("some_unknown_tool_xyz")


class TestGetToolMetaOverrides:
    def test_override_max_result_chars(self):
        meta = get_tool_meta("grep_search", agent_tool_overrides={"max_result_chars": 5000})
        assert meta.max_result_chars == 5000

    def test_override_category(self):
        meta = get_tool_meta("grep_search", agent_tool_overrides={"category": "custom_search"})
        assert meta.category == "custom_search"

    def test_override_concurrency_safe(self):
        meta = get_tool_meta("shell_tool", agent_tool_overrides={"is_concurrency_safe": True})
        assert meta.is_concurrency_safe is True

    def test_none_values_ignored(self):
        meta_no_override = get_tool_meta("grep_search")
        meta_with_none = get_tool_meta("grep_search", agent_tool_overrides={"max_result_chars": None})
        assert meta_with_none.max_result_chars == meta_no_override.max_result_chars

    def test_unknown_override_fields_ignored(self):
        meta = get_tool_meta("grep_search", agent_tool_overrides={"unknown_field": "ignored"})
        assert not hasattr(meta, "unknown_field")


class TestToolSpecDataclass:
    def test_frozen(self):
        meta = get_tool_meta("grep_search")
        with pytest.raises(FrozenInstanceError):
            meta.name = "modified"
