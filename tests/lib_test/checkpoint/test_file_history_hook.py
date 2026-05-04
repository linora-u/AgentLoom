"""
Tests for src.lib.checkpoint.file_history_hook.

Covers:
- Hook intercepts file-modifying tools (edit_file, write_file, etc.)
- Hook ignores non-file-modifying tools (read_file, grep_search, etc.)
- Hook handles missing file_path argument gracefully
- Hook handles track_edit exceptions gracefully
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.lib.checkpoint.file_history_hook import (
    FILE_MODIFYING_TOOLS,
    FileHistoryHook,
)


@pytest.fixture
def mock_fh():
    """Create a mock FileHistoryManager."""
    fh = MagicMock()
    fh.track_edit = MagicMock()
    return fh


@pytest.fixture
def hook(mock_fh):
    """Create a FileHistoryHook with step_number=0."""
    return FileHistoryHook(mock_fh, get_step_number=lambda: 0)


class TestFileHistoryHook:
    """Tests for FileHistoryHook.__call__."""

    def test_edit_file_triggers_backup(self, hook, mock_fh):
        """Normal: edit_file tool triggers track_edit."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="edit_file",
            tool_input={"file_path": "/tmp/test.py"},
        )
        mock_fh.track_edit.assert_called_once_with("/tmp/test.py", 0)

    def test_write_file_triggers_backup(self, hook, mock_fh):
        """Normal: write_file tool triggers track_edit."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="write_file",
            tool_input={"file_path": "/tmp/output.txt"},
        )
        mock_fh.track_edit.assert_called_once()

    def test_create_file_triggers_backup(self, hook, mock_fh):
        """Normal: create_file tool triggers track_edit."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="create_file",
            tool_input={"file_path": "/tmp/new.py"},
        )
        mock_fh.track_edit.assert_called_once()

    def test_read_file_does_not_trigger_backup(self, hook, mock_fh):
        """Normal: read_file is not a file-modifying tool."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="read_file",
            tool_input={"file_path": "/tmp/test.py"},
        )
        mock_fh.track_edit.assert_not_called()

    def test_grep_search_does_not_trigger(self, hook, mock_fh):
        """Normal: grep_search is read-only."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="grep_search",
            tool_input={"query": "def foo"},
        )
        mock_fh.track_edit.assert_not_called()

    def test_shell_tool_does_not_trigger(self, hook, mock_fh):
        """Normal: shell_tool is not in FILE_MODIFYING_TOOLS."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="shell_tool",
            tool_input={"command": "ls"},
        )
        mock_fh.track_edit.assert_not_called()

    def test_missing_file_path_no_op(self, hook, mock_fh):
        """Boundary: Tool with no file_path argument → no-op."""
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="edit_file",
            tool_input={"content": "new content"},
        )
        mock_fh.track_edit.assert_not_called()

    def test_track_edit_exception_does_not_propagate(self, hook, mock_fh):
        """Error: track_edit raises but hook does not propagate."""
        mock_fh.track_edit.side_effect = IOError("disk full")
        # Should not raise.
        result = hook(
            event_type="PRE_TOOL_USE",
            tool_name="edit_file",
            tool_input={"file_path": "/tmp/test.py"},
        )
        assert result is None

    def test_alternative_path_param(self, mock_fh):
        """Normal: Hook checks 'path' param as fallback."""
        hook = FileHistoryHook(mock_fh)
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="write_file",
            tool_input={"path": "/tmp/test.py"},
        )
        mock_fh.track_edit.assert_called_once()

    def test_filePath_param(self, mock_fh):
        """Normal: Hook checks 'filePath' camelCase param."""
        hook = FileHistoryHook(mock_fh)
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="edit_file",
            tool_input={"filePath": "/tmp/test.py"},
        )
        mock_fh.track_edit.assert_called_once()

    def test_step_number_from_callable(self, mock_fh):
        """Normal: Step number comes from the callable."""
        counter = {"n": 5}
        hook = FileHistoryHook(mock_fh, get_step_number=lambda: counter["n"])
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="edit_file",
            tool_input={"file_path": "/tmp/test.py"},
        )
        mock_fh.track_edit.assert_called_once_with("/tmp/test.py", 5)

    def test_all_file_modifying_tools_recognized(self, mock_fh):
        """Verify all tools in FILE_MODIFYING_TOOLS trigger backup."""
        hook = FileHistoryHook(mock_fh)
        for tool_name in FILE_MODIFYING_TOOLS:
            mock_fh.reset_mock()
            hook(
                event_type="PRE_TOOL_USE",
                tool_name=tool_name,
                tool_input={"file_path": "/tmp/test.py"},
            )
            assert mock_fh.track_edit.called, f"{tool_name} did not trigger backup"
