"""Tests for empty-result injection in tool_shim.inject_hooks.

When a tool's forward() returns None, empty string, or whitespace-only
string, the shim injects a marker message so the LLM never receives an
empty tool_result (which can trigger stop sequences).
"""

import pytest
from unittest.mock import MagicMock, patch

from src.lib.smolagents.hooks.tool_shim import inject_hooks, HOOKS_INJECTED_ATTR
from src.lib.smolagents.hooks.types import HookResult


def _make_mock_tool(name: str, forward_return_value):
    """Create a minimal mock Tool with a controllable forward() return."""
    tool = MagicMock()
    tool.name = name
    tool.inputs = {}
    # Clear the hooks-injected marker so inject_hooks will wrap it
    delattr_safe = getattr(tool, HOOKS_INJECTED_ATTR, None)
    if hasattr(tool, HOOKS_INJECTED_ATTR):
        delattr(tool, HOOKS_INJECTED_ATTR)
    # Reset the attribute to False to allow injection
    tool._hooks_injected = False
    tool.forward = MagicMock(return_value=forward_return_value)
    # Ensure hasattr works
    tool.forward.__name__ = "forward"
    tool.forward.__wrapped__ = None
    return tool


def _make_passthrough_hook_result():
    """Create a HookResult that allows the call through without modification."""
    return HookResult(success=True, decision="allow")


def _setup_injected_tool(tool_name: str, forward_return):
    """Inject hooks into a mock tool and return it, with all hooks mocked."""
    tool = _make_mock_tool(tool_name, forward_return)
    passthrough = _make_passthrough_hook_result()

    with patch(
        "src.lib.smolagents.hooks.tool_shim._resolve_hook_manager"
    ) as mock_hm, patch(
        "src.tools.tool_meta.get_tool_meta",
        side_effect=Exception("skip truncation"),
    ):
        hm_instance = MagicMock()
        hm_instance.trigger_hooks.return_value = passthrough
        hm_instance.flush_user_messages = MagicMock()
        mock_hm.return_value = hm_instance

        inject_hooks(tool)
        # Now call the wrapped forward
        result = tool.forward()
    return result


# ---------------------------------------------------------------------------
# Normal cases
# ---------------------------------------------------------------------------

class TestEmptyResultInjection:
    """Empty / None / whitespace-only results get a marker injected."""

    def test_empty_string_gets_marker(self):
        result = _setup_injected_tool("my_tool", "")
        assert result == "(my_tool completed with no output)"

    def test_none_gets_marker(self):
        result = _setup_injected_tool("my_tool", None)
        assert result == "(my_tool completed with no output)"

    def test_whitespace_only_gets_marker(self):
        result = _setup_injected_tool("my_tool", "   \n  ")
        assert result == "(my_tool completed with no output)"

    def test_tabs_and_newlines_only(self):
        result = _setup_injected_tool("my_tool", "\t\n\r\n")
        assert result == "(my_tool completed with no output)"


# ---------------------------------------------------------------------------
# Non-empty results — unchanged
# ---------------------------------------------------------------------------

class TestNonEmptyResultUnchanged:
    """Non-empty tool results should pass through without modification."""

    def test_normal_string_unchanged(self):
        result = _setup_injected_tool("my_tool", "Found 3 matches")
        assert result == "Found 3 matches"

    def test_no_matches_message_unchanged(self):
        """'No matches found.' is non-empty and should be preserved."""
        result = _setup_injected_tool("grep_tool", "No matches found.")
        assert result == "No matches found."

    def test_single_char_unchanged(self):
        result = _setup_injected_tool("my_tool", "x")
        assert result == "x"

    def test_multiline_result_unchanged(self):
        text = "line1\nline2\nline3"
        result = _setup_injected_tool("my_tool", text)
        assert result == text


# ---------------------------------------------------------------------------
# Tool name appears in marker
# ---------------------------------------------------------------------------

class TestMarkerIncludesToolName:
    """The injected marker message should contain the tool name."""

    @pytest.mark.parametrize("tool_name", [
        "grep_search",
        "shell_tool",
        "read_file",
    ])
    def test_different_tool_names_in_marker(self, tool_name):
        result = _setup_injected_tool(tool_name, "")
        assert tool_name in result
        assert "completed with no output" in result


# ---------------------------------------------------------------------------
# Hooks injection idempotency
# ---------------------------------------------------------------------------

class TestHooksInjectionIdempotency:
    """inject_hooks should not double-wrap a tool."""

    def test_already_injected_skipped(self):
        tool = _make_mock_tool("my_tool", "result")
        original_forward = tool.forward

        with patch(
            "src.lib.smolagents.hooks.tool_shim._resolve_hook_manager"
        ) as mock_hm:
            hm_instance = MagicMock()
            hm_instance.trigger_hooks.return_value = _make_passthrough_hook_result()
            hm_instance.flush_user_messages = MagicMock()
            mock_hm.return_value = hm_instance

            inject_hooks(tool)
            first_forward = tool.forward

            # Second injection should be a no-op
            inject_hooks(tool)
            assert tool.forward is first_forward


# ---------------------------------------------------------------------------
# Tool without forward method
# ---------------------------------------------------------------------------

class TestToolWithoutForward:
    """Tools without a forward attribute should be returned unchanged."""

    def test_no_forward_attribute(self):
        tool = MagicMock(spec=[])  # no attributes at all
        tool.name = "broken_tool"
        tool._hooks_injected = False
        # Should not raise
        returned = inject_hooks(tool)
        assert returned is tool
