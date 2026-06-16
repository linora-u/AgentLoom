"""Tests for large-result persistence (truncation) in tool_shim.

When a tool returns a string exceeding the configured max_result_chars
threshold, the shim persists the full result to a temp file and returns
a preview message containing a file path and a truncated preview.
"""

import os
import pytest
from unittest.mock import MagicMock, patch

from src.lib.smolagents.hooks.tool_shim import (
    inject_hooks,
    _persist_large_result,
    _PREVIEW_SIZE,
    HOOKS_INJECTED_ATTR,
)
from src.lib.smolagents.hooks.types import HookResult
from src.tools.shell.output_interceptor import OutputInterceptor
from src.tools.tool_meta import ToolMeta, get_tool_meta


def _make_mock_tool(name: str, forward_return_value):
    """Create a minimal mock Tool with controllable forward()."""
    tool = MagicMock()
    tool.name = name
    tool.inputs = {}
    tool._hooks_injected = False
    tool.forward = MagicMock(return_value=forward_return_value)
    tool.forward.__name__ = "forward"
    tool.forward.__wrapped__ = None
    return tool


def _make_passthrough_hook_result():
    return HookResult(success=True, decision="allow")


def _run_tool_with_meta(tool_name, forward_return, meta):
    """Inject hooks into a mock tool with a specific ToolMeta and call forward()."""
    tool = _make_mock_tool(tool_name, forward_return)
    passthrough = _make_passthrough_hook_result()

    with patch(
        "src.lib.smolagents.hooks.tool_shim._resolve_hook_manager"
    ) as mock_hm, patch(
        "src.tools.tool_meta.get_tool_meta",
        return_value=meta,
    ):
        hm_instance = MagicMock()
        hm_instance.trigger_hooks.return_value = passthrough
        hm_instance.flush_user_messages = MagicMock()
        mock_hm.return_value = hm_instance

        inject_hooks(tool)
        result = tool.forward()
    return result


# ---------------------------------------------------------------------------
# Direct _persist_large_result tests
# ---------------------------------------------------------------------------

class TestPersistLargeResult:
    """Test the low-level _persist_large_result helper."""

    def test_creates_file_and_returns_preview(self, tmp_path, monkeypatch):
        """Result is written to disk; preview message is returned."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        big_text = "A" * 50000
        preview_msg = _persist_large_result("test_tool", big_text, 20000)

        # Preview message format assertions
        assert "Output too large" in preview_msg
        assert "Preview" in preview_msg
        assert str(tmp_path) in preview_msg

        # Verify the file was actually written
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big_text

    def test_preview_contains_first_n_chars(self, tmp_path, monkeypatch):
        """The preview should contain the first _PREVIEW_SIZE characters."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        content = "X" * 50000
        preview_msg = _persist_large_result("tool", content, 20000)
        # The preview section should contain _PREVIEW_SIZE Xs
        assert "X" * _PREVIEW_SIZE in preview_msg

    def test_small_content_no_ellipsis(self, tmp_path, monkeypatch):
        """When content <= _PREVIEW_SIZE, no ellipsis should appear at the end."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        content = "Y" * _PREVIEW_SIZE  # exactly _PREVIEW_SIZE
        preview_msg = _persist_large_result("tool", content, 1000)
        # There should be no trailing "..." since content fits in preview
        assert not preview_msg.rstrip().endswith("...")

    def test_large_content_has_ellipsis(self, tmp_path, monkeypatch):
        """When content > _PREVIEW_SIZE, the preview ends with '...'."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        content = "Z" * (_PREVIEW_SIZE + 100)
        preview_msg = _persist_large_result("tool", content, 1000)
        assert preview_msg.rstrip().endswith("...")

    def test_filename_contains_tool_name(self, tmp_path, monkeypatch):
        """The persisted file name should include the tool name."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        _persist_large_result("grep_search", "data" * 5000, 1000)
        files = list(tmp_path.iterdir())
        assert any("grep_search" in f.name for f in files)

    def test_threshold_info_in_preview(self, tmp_path, monkeypatch):
        """The preview message should mention the threshold value."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        preview_msg = _persist_large_result("tool", "x" * 30000, 20000)
        assert "20000" in preview_msg


# ---------------------------------------------------------------------------
# Integration: large result through inject_hooks
# ---------------------------------------------------------------------------

class TestLargeResultTruncation:
    """Large results trigger persistence + preview via the hook shim."""

    def test_result_exceeds_threshold_gets_preview(self, tmp_path, monkeypatch):
        """Result > threshold → persisted to file, LLM gets preview message."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        big_text = "B" * 50000
        meta = ToolMeta(name="big_tool", max_result_chars=20000)

        result = _run_tool_with_meta("big_tool", big_text, meta)

        assert "Output too large" in result
        assert "Preview" in result
        assert str(tmp_path) in result

        # Full output is in the file
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big_text

    def test_result_within_threshold_unchanged(self):
        """Result <= threshold → returned as-is, no truncation."""
        small_text = "Hello world"
        meta = ToolMeta(name="small_tool", max_result_chars=20000)

        result = _run_tool_with_meta("small_tool", small_text, meta)
        assert result == small_text

    def test_max_result_chars_none_no_truncation(self):
        """max_result_chars=None → never truncate, even for huge results."""
        big_text = "C" * 100000
        meta = ToolMeta(name="exempt_tool", max_result_chars=None)

        result = _run_tool_with_meta("exempt_tool", big_text, meta)
        assert result == big_text

    def test_exactly_at_threshold_unchanged(self):
        """Result exactly at threshold → not truncated (> is required)."""
        threshold = 1000
        exact_text = "D" * threshold
        meta = ToolMeta(name="edge_tool", max_result_chars=threshold)

        result = _run_tool_with_meta("edge_tool", exact_text, meta)
        assert result == exact_text

    def test_one_over_threshold_truncated(self, tmp_path, monkeypatch):
        """Result at threshold + 1 → triggers truncation."""
        monkeypatch.setattr(
            "src.lib.smolagents.hooks.tool_shim._TOOL_RESULTS_DIR",
            str(tmp_path),
        )
        threshold = 1000
        text = "E" * (threshold + 1)
        meta = ToolMeta(name="edge_tool", max_result_chars=threshold)

        result = _run_tool_with_meta("edge_tool", text, meta)
        assert "Output too large" in result

    def test_shell_tool_notice_survives_outer_shim_threshold(self, tmp_path):
        """shell_tool's own artifact notice must remain visible to agents."""
        interceptor = OutputInterceptor(preview_bytes=30000, storage_dir=str(tmp_path))
        interceptor.write("\n".join(str(i) for i in range(1, 12001)))
        shell_result = interceptor.finalize()

        assert "<system_notice>" in shell_result
        assert "FULL, unbroken output log" in shell_result

        meta = get_tool_meta("shell_tool")
        assert meta.max_result_chars is None or len(shell_result) <= meta.max_result_chars


# ---------------------------------------------------------------------------
# get_tool_meta failure → graceful fallback (no truncation)
# ---------------------------------------------------------------------------

class TestTruncationMetaFailure:
    """When get_tool_meta raises, truncation is skipped gracefully."""

    def test_meta_error_no_truncation(self):
        """If get_tool_meta fails, the original result is returned."""
        big_text = "F" * 50000
        tool = _make_mock_tool("failing_meta_tool", big_text)
        passthrough = _make_passthrough_hook_result()

        with patch(
            "src.lib.smolagents.hooks.tool_shim._resolve_hook_manager"
        ) as mock_hm, patch(
            "src.tools.tool_meta.get_tool_meta",
            side_effect=Exception("config unavailable"),
        ):
            hm_instance = MagicMock()
            hm_instance.trigger_hooks.return_value = passthrough
            hm_instance.flush_user_messages = MagicMock()
            mock_hm.return_value = hm_instance

            inject_hooks(tool)
            result = tool.forward()

        # Truncation skipped: original large result returned
        assert result == big_text
