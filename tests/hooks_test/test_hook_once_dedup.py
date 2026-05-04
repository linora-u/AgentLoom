"""Tests for hook once-flag auto-removal and deduplication.

Validates:
- once=True hooks are removed after execution
- Dedup prevents duplicate registrations
- Once hooks are removed even on failure
"""

import unittest

from src.lib.smolagents.hooks import HookEvent, HookManager, HookResult
from src.lib.smolagents.hooks.types import HookContext


def _counter_hook(counters: dict):
    """Factory: returns a hook that increments a counter."""
    def hook(context: HookContext) -> HookResult:
        counters["calls"] = counters.get("calls", 0) + 1
        return HookResult(success=True, decision="allow")
    hook.__name__ = "_counter_hook"
    return hook


def _failing_hook(context: HookContext) -> HookResult:
    """Hook that raises an exception."""
    raise RuntimeError("intentional failure")


class TestHookOnce(unittest.TestCase):
    """Once-flag auto-removal tests."""

    def setUp(self):
        self.manager = HookManager()

    def test_once_hook_removed_after_execution(self):
        """A hook with once=True should be removed after its first execution."""
        counters = {}
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _counter_hook(counters),
            once=True,
        )
        # First call — hook should execute
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(counters["calls"], 1)

        # Second call — hook should be gone
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(counters["calls"], 1)  # No increment

    def test_once_hook_removed_even_on_exception(self):
        """A once-hook that raises should still be removed."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _failing_hook,
            once=True,
        )
        hooks_before = len(self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE))
        self.assertEqual(hooks_before, 1)

        # Execute — exception is caught internally
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})

        hooks_after = len(self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE))
        self.assertEqual(hooks_after, 0)

    def test_non_once_hook_persists(self):
        """A regular hook (once=False) should persist across calls."""
        counters = {}
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _counter_hook(counters),
            once=False,
        )
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(counters["calls"], 2)

    def test_mixed_once_and_persistent_hooks(self):
        """Once hooks are removed while persistent hooks remain."""
        once_counters = {}
        persist_counters = {}
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _counter_hook(once_counters),
            once=True,
        )
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _counter_hook(persist_counters),
            once=False,
        )
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})

        self.assertEqual(once_counters["calls"], 1)
        self.assertEqual(persist_counters["calls"], 2)


class TestHookDedup(unittest.TestCase):
    """Hook deduplication tests."""

    def setUp(self):
        self.manager = HookManager()

    def test_duplicate_prevented_when_allow_duplicates_false(self):
        """Registering the same function twice with allow_duplicates=False
        should keep only one copy."""
        counters = {}
        hook_fn = _counter_hook(counters)
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", hook_fn,
            allow_duplicates=False, source="test",
        )
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", hook_fn,
            allow_duplicates=False, source="test",
        )
        hooks = self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)
        self.assertEqual(len(hooks), 1)

    def test_duplicates_allowed_by_default(self):
        """By default, duplicate registrations are allowed."""
        counters = {}
        hook_fn = _counter_hook(counters)
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook_fn)
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook_fn)
        hooks = self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)
        self.assertEqual(len(hooks), 2)


class TestHookManagerDebugInterface(unittest.TestCase):
    """Tests for debug/inspection interfaces."""

    def setUp(self):
        self.manager = HookManager()

    def test_get_registered_hooks_empty(self):
        hooks = self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)
        self.assertEqual(len(hooks), 0)

    def test_clear_hooks_removes_all(self):
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*",
            lambda ctx: HookResult(success=True, decision="allow"),
        )
        self.manager.register_hook(
            HookEvent.STOP, "*",
            lambda ctx: HookResult(success=True, decision="allow"),
        )
        self.manager.clear_hooks()
        self.assertEqual(len(self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)), 0)
        self.assertEqual(len(self.manager.get_registered_hooks(HookEvent.STOP)), 0)

    def test_remove_hook_by_function(self):
        def my_hook(context: HookContext) -> HookResult:
            return HookResult(success=True, decision="allow")

        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", my_hook)
        self.assertEqual(len(self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)), 1)

        removed = self.manager.remove_hook(HookEvent.PRE_TOOL_USE, my_hook)
        self.assertTrue(removed)
        self.assertEqual(len(self.manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)), 0)

    def test_remove_nonexistent_hook(self):
        removed = self.manager.remove_hook(
            HookEvent.PRE_TOOL_USE,
            lambda ctx: HookResult(success=True, decision="allow"),
        )
        self.assertFalse(removed)

    def test_disable_enable_hooks(self):
        """Disabled hooks should return default allow without executing."""
        counters = {}
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _counter_hook(counters),
        )
        self.manager.disable_hooks()
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")
        self.assertEqual(counters.get("calls", 0), 0)  # Not executed

        self.manager.enable_hooks()
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(counters["calls"], 1)  # Now executed


if __name__ == "__main__":
    unittest.main()
