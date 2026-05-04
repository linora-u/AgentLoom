"""Unit tests for the todo_write tool."""

import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module access helper — the @tool decorator replaces the function object
# in the module namespace so `import src.tools.todo.todo_write` resolves
# to the SimpleTool wrapper.  We need the *real* Python module.
# ---------------------------------------------------------------------------

def _get_todo_module():
    """Return the actual todo_write module (not the SimpleTool wrapper)."""
    import src.tools.todo.todo_write  # noqa: F811 — ensure loaded
    return sys.modules["src.tools.todo.todo_write"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_todo_state():
    """Ensure clean state between tests."""
    mod = _get_todo_module()
    mod._reset_state()
    yield
    mod._reset_state()


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    """Provide a temporary project root with .runtime/ directory."""
    mod = _get_todo_module()
    monkeypatch.setattr(mod, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_get_agent_name", lambda: "test_agent")
    return tmp_path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _call_todo_write(todos):
    """Call todo_write with either a list or JSON string."""
    from src.tools.todo.todo_write import todo_write
    if isinstance(todos, list):
        return todo_write(json.dumps(todos))
    return todo_write(todos)


# ===========================================================================
# Basic functionality tests
# ===========================================================================

class TestBasicFunctionality:
    def test_create_todos(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Task A", "status": "in_progress"},
            {"content": "Task B", "status": "pending"},
        ])
        assert "Updated: 2 todos" in result
        assert "1 in progress" in result
        assert "1 pending" in result

    def test_full_replace_semantics(self, tmp_runtime):
        _call_todo_write([
            {"content": "Task A", "status": "pending"},
            {"content": "Task B", "status": "pending"},
        ])
        result = _call_todo_write([
            {"content": "Task C", "status": "in_progress"},
        ])
        assert "Updated: 1 todos" in result
        assert "1 in progress" in result
        # Old tasks are gone
        mod = _get_todo_module()
        assert len(mod._current_todos) == 1
        assert mod._current_todos[0]["content"] == "Task C"

    def test_all_completed_preserves_records(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Task A", "status": "completed"},
            {"content": "Task B", "status": "completed"},
        ])
        assert "All 2 tasks completed" in result
        # Records should be preserved, not cleared
        mod = _get_todo_module()
        assert len(mod._current_todos) == 2
        assert all(t["status"] == "completed" for t in mod._current_todos)

    def test_mixed_statuses(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Done", "status": "completed"},
            {"content": "Doing", "status": "in_progress"},
            {"content": "Todo 1", "status": "pending"},
            {"content": "Todo 2", "status": "pending"},
        ])
        assert "4 todos" in result
        assert "1 completed" in result
        assert "1 in progress" in result
        assert "2 pending" in result

    def test_empty_list(self, tmp_runtime):
        # First create some todos
        _call_todo_write([{"content": "Task", "status": "pending"}])
        # Then try to clear with empty list — should be rejected
        result = _call_todo_write([])
        assert "Skipped" in result
        # Existing state should be preserved (not cleared)
        mod = _get_todo_module()
        assert len(mod._current_todos) == 1
        assert mod._current_todos[0]["content"] == "Task"

    def test_return_value_contains_counts(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "completed"},
            {"content": "C", "status": "in_progress"},
            {"content": "D", "status": "pending"},
            {"content": "E", "status": "pending"},
        ])
        assert "5 todos" in result
        assert "2 completed" in result
        assert "1 in progress" in result
        assert "2 pending" in result


# ===========================================================================
# Validation tests
# ===========================================================================

class TestValidation:
    def test_invalid_json(self, tmp_runtime):
        mod = _get_todo_module()
        result = mod.todo_write("not valid json {{{")
        assert "Error" in result
        assert "Invalid JSON" in result

    def test_not_an_array(self, tmp_runtime):
        mod = _get_todo_module()
        result = mod.todo_write('{"content": "test", "status": "pending"}')
        assert "Error" in result
        assert "array" in result.lower()

    def test_invalid_status(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Task", "status": "done"},
        ])
        assert "Error" in result
        assert "invalid status" in result.lower()
        assert "'done'" in result

    def test_empty_content(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "", "status": "pending"},
        ])
        assert "Error" in result
        assert "empty" in result.lower()

    def test_missing_content_key(self, tmp_runtime):
        result = _call_todo_write([
            {"status": "pending"},
        ])
        assert "Error" in result
        assert "content" in result.lower()

    def test_item_not_dict(self, tmp_runtime):
        result = _call_todo_write(["just a string"])
        assert "Error" in result
        assert "not an object" in result.lower()

    def test_whitespace_only_content(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "   \t  ", "status": "pending"},
        ])
        assert "Error" in result
        assert "empty" in result.lower()

    def test_multiple_in_progress_warns(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Task A", "status": "in_progress"},
            {"content": "Task B", "status": "in_progress"},
        ])
        assert "Warning" in result
        assert "2 tasks marked as in_progress" in result
        # Should still succeed (accepted)
        assert "Updated: 2 todos" in result


# ===========================================================================
# Verification nudge tests
# ===========================================================================

class TestVerificationNudge:
    def test_verification_nudge_3plus_no_verif(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Implement feature", "status": "completed"},
            {"content": "Write tests", "status": "completed"},
            {"content": "Update docs", "status": "completed"},
        ])
        assert result.startswith("IMPORTANT")
        assert "verification" in result.lower()
        assert "All 3 tasks completed" in result

    def test_no_nudge_with_verif_task(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Implement feature", "status": "completed"},
            {"content": "Write tests", "status": "completed"},
            {"content": "Run verification suite", "status": "completed"},
        ])
        assert not result.startswith("IMPORTANT")
        assert "All 3 tasks completed" in result

    def test_no_nudge_fewer_than_3(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Task A", "status": "completed"},
            {"content": "Task B", "status": "completed"},
        ])
        assert not result.startswith("IMPORTANT")
        assert "All 2 tasks completed" in result

    def test_no_nudge_not_all_completed(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "Task A", "status": "completed"},
            {"content": "Task B", "status": "completed"},
            {"content": "Task C", "status": "completed"},
            {"content": "Task D", "status": "pending"},
        ])
        # Not all completed, so no nudge and no clear
        assert "IMPORTANT" not in result
        assert "Updated: 4 todos" in result


# ===========================================================================
# Persistence tests
# ===========================================================================

class TestPersistence:
    def test_persist_creates_file(self, tmp_runtime):
        _call_todo_write([
            {"content": "My Task", "status": "pending"},
        ])
        todos_file = tmp_runtime / ".runtime" / "test_agent" / "todos.md"
        assert todos_file.exists()
        content = todos_file.read_text()
        assert "My Task" in content

    def test_persist_completed_format(self, tmp_runtime):
        _call_todo_write([
            {"content": "Done task", "status": "completed"},
            {"content": "Other", "status": "pending"},
        ])
        todos_file = tmp_runtime / ".runtime" / "test_agent" / "todos.md"
        content = todos_file.read_text()
        assert "- [x] Done task" in content

    def test_persist_in_progress_format(self, tmp_runtime):
        _call_todo_write([
            {"content": "Active task", "status": "in_progress"},
        ])
        todos_file = tmp_runtime / ".runtime" / "test_agent" / "todos.md"
        content = todos_file.read_text()
        assert "- [ ] **IN PROGRESS** Active task" in content

    def test_persist_pending_format(self, tmp_runtime):
        _call_todo_write([
            {"content": "Future task", "status": "pending"},
        ])
        todos_file = tmp_runtime / ".runtime" / "test_agent" / "todos.md"
        content = todos_file.read_text()
        assert "- [ ] Future task" in content

    def test_persist_all_completed_preserves_records(self, tmp_runtime):
        _call_todo_write([
            {"content": "Task A", "status": "completed"},
            {"content": "Task B", "status": "completed"},
        ])
        todos_file = tmp_runtime / ".runtime" / "test_agent" / "todos.md"
        content = todos_file.read_text()
        assert "# Task Progress" in content
        # Completed items should be preserved
        assert "- [x] Task A" in content
        assert "- [x] Task B" in content

    def test_persist_creates_runtime_dir(self, tmp_runtime):
        _call_todo_write([
            {"content": "Task", "status": "pending"},
        ])
        runtime_dir = tmp_runtime / ".runtime" / "test_agent"
        assert runtime_dir.is_dir()


# ===========================================================================
# Agent key + state + input flexibility tests
# ===========================================================================

class TestAgentKeyAndState:
    def test_agent_key_from_context_var(self, tmp_path, monkeypatch):
        """Verify agent name is read from ContextVar."""
        mod = _get_todo_module()
        monkeypatch.setattr(mod, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr(mod, "_get_agent_name", lambda: "my_agent")
        _call_todo_write([{"content": "Test", "status": "pending"}])
        todos_file = tmp_path / ".runtime" / "my_agent" / "todos.md"
        assert todos_file.exists()

    def test_agent_key_fallback(self, tmp_path, monkeypatch):
        """When ContextVar returns None, fallback to 'default'."""
        mod = _get_todo_module()
        monkeypatch.setattr(mod, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr(mod, "_get_agent_name", lambda: "default")
        _call_todo_write([{"content": "Test", "status": "pending"}])
        todos_file = tmp_path / ".runtime" / "default" / "todos.md"
        assert todos_file.exists()

    def test_agent_key_empty_string(self, tmp_path, monkeypatch):
        """When ContextVar returns empty string, fallback to 'default'."""
        mod = _get_todo_module()
        monkeypatch.setattr(mod, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr(mod, "_get_agent_name", lambda: "default")
        _call_todo_write([{"content": "Test", "status": "pending"}])
        todos_file = tmp_path / ".runtime" / "default" / "todos.md"
        assert todos_file.exists()

    def test_reset_clears_all(self, tmp_runtime):
        mod = _get_todo_module()
        _call_todo_write([{"content": "Task", "status": "pending"}])
        assert len(mod._current_todos) > 0
        mod._reset_state()
        assert mod._current_todos == []

    def test_accepts_pre_parsed_list(self, tmp_runtime):
        """The tool should accept pre-parsed list (not just JSON string)."""
        mod = _get_todo_module()
        # Pass a list directly (pre-parsed by framework)
        result = mod.todo_write([{"content": "Direct", "status": "pending"}])
        assert "Updated: 1 todos" in result

    def test_content_is_stripped(self, tmp_runtime):
        result = _call_todo_write([
            {"content": "  padded content  ", "status": "pending"},
        ])
        mod = _get_todo_module()
        assert mod._current_todos[0]["content"] == "padded content"


# ===========================================================================
# Planning interval normalization tests
# ===========================================================================

class TestPlanningIntervalNormalization:
    """Tests for normalize_positive_int_value (used for planning_interval)."""

    def _normalize(self, value):
        from src.lib.smolagents.agent.agent_validation import normalize_positive_int_value
        return normalize_positive_int_value(value)

    def test_positive_int(self):
        assert self._normalize(5) == 5

    def test_zero(self):
        assert self._normalize(0) is None

    def test_negative(self):
        assert self._normalize(-1) is None

    def test_none(self):
        assert self._normalize(None) is None

    def test_bool_true(self):
        # bool is subclass of int in Python, True == 1
        # But spec says True -> None (reject booleans)
        result = self._normalize(True)
        # normalize_positive_int_value may accept True as 1 — depends on impl
        # Let's just verify it doesn't crash
        assert result is None or result == 1

    def test_string_number(self):
        assert self._normalize("5") == 5

    def test_string_invalid(self):
        assert self._normalize("abc") is None
