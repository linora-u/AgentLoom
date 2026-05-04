"""Tests for tool metadata resolution: resolve_tool_function and get_tool_meta.

Covers convention-based function lookup, YAML-driven metadata assembly,
agent YAML overrides, and error cases.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.tools.tool_meta import (
    resolve_tool_function,
    get_tool_meta,
    ToolMeta,
    _HARDCODED_DEFAULTS,
)


# ---------------------------------------------------------------------------
# resolve_tool_function — normal cases
# ---------------------------------------------------------------------------

class TestResolveToolFunctionNormal:
    """Convention-based lookup of tool functions in src.tools."""

    def test_grep_search_resolves(self):
        func = resolve_tool_function("grep_search")
        assert callable(func)

    def test_glob_search_resolves(self):
        func = resolve_tool_function("glob_search")
        assert callable(func)

    def test_read_file_resolves(self):
        func = resolve_tool_function("read_file")
        assert callable(func)

    def test_edit_file_resolves(self):
        func = resolve_tool_function("edit_file")
        assert callable(func)

    def test_shell_tool_resolves(self):
        func = resolve_tool_function("shell_tool")
        assert callable(func)

    def test_resolved_function_is_actual_function(self):
        """The resolved object should be a real callable, not a class or module."""
        func = resolve_tool_function("grep_search")
        assert callable(func)
        # It should have a __name__ attribute (functions do)
        assert hasattr(func, "__name__") or hasattr(func, "__call__")


# ---------------------------------------------------------------------------
# resolve_tool_function — error cases
# ---------------------------------------------------------------------------

class TestResolveToolFunctionErrors:
    """Non-existent tool names should raise ValueError."""

    def test_nonexistent_tool_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_tool_function("nonexistent_tool_xyz")

    def test_error_message_lists_available_tools(self):
        """The error message should list some available tool names."""
        with pytest.raises(ValueError) as exc_info:
            resolve_tool_function("totally_fake_tool")
        error_msg = str(exc_info.value)
        # Should mention at least one known tool
        assert "grep_search" in error_msg or "Available tools" in error_msg

    def test_private_name_not_resolved(self):
        """Names starting with _ should not resolve (they are filtered)."""
        with pytest.raises(ValueError, match="not found"):
            resolve_tool_function("_private_helper")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_tool_function("")


# ---------------------------------------------------------------------------
# get_tool_meta — defaults
# ---------------------------------------------------------------------------

class TestGetToolMetaDefaults:
    """get_tool_meta should return ToolMeta with correct defaults."""

    def test_returns_toolmeta_instance(self):
        meta = get_tool_meta("grep_search")
        assert isinstance(meta, ToolMeta)

    def test_name_is_set(self):
        meta = get_tool_meta("grep_search")
        assert meta.name == "grep_search"

    def test_grep_search_has_correct_category(self):
        """grep_search is configured as 'search' category in system.yaml."""
        meta = get_tool_meta("grep_search")
        assert meta.category == "search"

    def test_grep_search_max_result_chars(self):
        """grep_search is configured with max_result_chars=20000."""
        meta = get_tool_meta("grep_search")
        assert meta.max_result_chars == 20000

    def test_grep_search_concurrency_safe(self):
        meta = get_tool_meta("grep_search")
        assert meta.is_concurrency_safe is True

    def test_shell_tool_not_concurrency_safe(self):
        """shell_tool is configured as not concurrency safe."""
        meta = get_tool_meta("shell_tool")
        assert meta.is_concurrency_safe is False

    def test_read_file_category(self):
        """read_file is configured with category=file_ops."""
        meta = get_tool_meta("read_file")
        assert meta.category == "file_ops"


# ---------------------------------------------------------------------------
# get_tool_meta — unconfigured tool uses default section
# ---------------------------------------------------------------------------

class TestGetToolMetaUnconfigured:
    """Tools without per-tool config should use the default section."""

    def test_unknown_tool_uses_default(self):
        """A tool with no specific config should use system.yaml defaults."""
        meta = get_tool_meta("some_unknown_tool_xyz")
        # Should still return a valid ToolMeta with default values
        assert isinstance(meta, ToolMeta)
        assert meta.name == "some_unknown_tool_xyz"
        # Uses the default max_result_chars from system.yaml default section
        assert meta.max_result_chars == _HARDCODED_DEFAULTS["max_result_chars"]

    def test_unknown_tool_default_category(self):
        meta = get_tool_meta("unknown_widget")
        assert meta.category == "general"


# ---------------------------------------------------------------------------
# get_tool_meta — agent YAML overrides
# ---------------------------------------------------------------------------

class TestGetToolMetaOverrides:
    """Agent YAML tool overrides should take highest precedence."""

    def test_override_max_result_chars(self):
        meta = get_tool_meta("grep_search", agent_tool_overrides={
            "max_result_chars": 5000,
        })
        assert meta.max_result_chars == 5000

    def test_override_category(self):
        meta = get_tool_meta("grep_search", agent_tool_overrides={
            "category": "custom_search",
        })
        assert meta.category == "custom_search"

    def test_override_concurrency_safe(self):
        meta = get_tool_meta("shell_tool", agent_tool_overrides={
            "is_concurrency_safe": True,
        })
        assert meta.is_concurrency_safe is True

    def test_override_with_none_values_ignored(self):
        """None values in overrides should not overwrite existing config."""
        meta_no_override = get_tool_meta("grep_search")
        meta_with_none = get_tool_meta("grep_search", agent_tool_overrides={
            "max_result_chars": None,
        })
        assert meta_with_none.max_result_chars == meta_no_override.max_result_chars

    def test_override_unknown_fields_ignored(self):
        """Unknown override fields should be silently ignored (not in ToolMeta)."""
        meta = get_tool_meta("grep_search", agent_tool_overrides={
            "unknown_field": "should_be_ignored",
        })
        assert isinstance(meta, ToolMeta)
        assert not hasattr(meta, "unknown_field")

    def test_empty_overrides(self):
        """Empty override dict should not change anything."""
        meta_default = get_tool_meta("grep_search")
        meta_empty = get_tool_meta("grep_search", agent_tool_overrides={})
        assert meta_default == meta_empty

    def test_none_overrides(self):
        """None overrides should not change anything."""
        meta_default = get_tool_meta("grep_search")
        meta_none = get_tool_meta("grep_search", agent_tool_overrides=None)
        assert meta_default == meta_none


# ---------------------------------------------------------------------------
# ToolMeta dataclass
# ---------------------------------------------------------------------------

class TestToolMetaDataclass:
    """ToolMeta dataclass invariants."""

    def test_frozen(self):
        """ToolMeta instances should be immutable (frozen=True)."""
        meta = ToolMeta(name="test")
        with pytest.raises(AttributeError):
            meta.name = "modified"

    def test_default_values(self):
        """Default field values should match hardcoded defaults."""
        meta = ToolMeta(name="test")
        assert meta.max_result_chars == 20000
        assert meta.is_concurrency_safe is True
        assert meta.category == "general"
        assert meta.disable_type_coercion is False
