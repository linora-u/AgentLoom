"""
Tests for src.lib.checkpoint.file_history_hook.

Covers:
- Hook intercepts file-modifying tools (edit_file, write_file, etc.)
- Hook ignores non-file-modifying tools (read_file, grep_search, etc.)
- Hook handles missing file_path argument gracefully
- Hook propagates track_edit exceptions to the fail-closed boundary
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.lib.checkpoint.file_history_hook import (
    FileHistoryHook,
    record_active_file_history,
)
from src.lib.smolagents.hooks.types import HookContext
from src.tools import list_tool_specs


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
        """Normal: shell_tool is not a destructive path tool."""
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

    def test_track_edit_exception_propagates_to_gate_policy(self, hook, mock_fh):
        """PreToolUse failures must reach HookRun so it can fail closed."""
        mock_fh.track_edit.side_effect = OSError("disk full")
        with pytest.raises(IOError, match="disk full"):
            hook(
                event_type="PRE_TOOL_USE",
                tool_name="edit_file",
                tool_input={"file_path": "/tmp/test.py"},
            )

    def test_destructive_tool_registry_failure_propagates_to_gate_policy(self, hook):
        with (
            patch("src.tools.list_tool_specs", side_effect=RuntimeError("registry unavailable")),
            pytest.raises(RuntimeError, match="registry unavailable"),
        ):
            hook(
                event_type="PRE_TOOL_USE",
                tool_name="write_file",
                tool_input={"file_path": "/tmp/test.py"},
            )

    def test_undeclared_path_param_ignored(self, mock_fh):
        """ToolSpec path_params are authoritative."""
        hook = FileHistoryHook(mock_fh)
        hook(
            event_type="PRE_TOOL_USE",
            tool_name="write_file",
            tool_input={"path": "/tmp/test.py"},
        )
        mock_fh.track_edit.assert_not_called()

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

    def test_step_number_from_hook_context(self, mock_fh):
        """Normal: HookContext step number is used by default."""
        hook = FileHistoryHook(mock_fh)
        context = HookContext(
            local_run_id="session",
            cwd="/tmp",
            hook_event_name="PreToolUse",
            tool_name="edit_file",
            tool_input={"file_path": "/tmp/test.py"},
            step_number=7,
        )

        hook(context)

        mock_fh.track_edit.assert_called_once_with("/tmp/test.py", 7)

    def test_all_file_modifying_tools_recognized(self, mock_fh):
        """Verify destructive registry tools with path params trigger backup."""
        hook = FileHistoryHook(mock_fh)
        tool_names = [spec.name for spec in list_tool_specs() if spec.is_destructive and spec.path_params]
        assert tool_names
        for tool_name in tool_names:
            mock_fh.reset_mock()
            hook(
                event_type="PRE_TOOL_USE",
                tool_name=tool_name,
                tool_input={"file_path": "/tmp/test.py"},
            )
            assert mock_fh.track_edit.called, f"{tool_name} did not trigger backup"

    def test_active_runtime_entry_records_supplied_final_input(self, mock_fh):
        coordinator = MagicMock()
        coordinator._file_history = mock_fh

        with patch(
            "src.lib.checkpoint.coordinator.CheckpointCoordinator.current",
            return_value=coordinator,
        ):
            record_active_file_history(
                tool_name="write_file",
                tool_input={"file_path": "/tmp/final.txt", "content": "final"},
                step_number=9,
            )

        mock_fh.track_edit.assert_called_once_with("/tmp/final.txt", 9)
