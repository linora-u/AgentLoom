"""Tests for hook timeout enforcement.

Validates:
- Normal: timed-out hook returns meaningful reason string
- Error: infinite-loop hook is terminated
- Boundary: very short timeout triggers immediately
- Regression: timed-out Python hooks use daemon workers
"""

import threading
import time
import unittest
from unittest.mock import patch

from src.lib.smolagents.hooks import HookEvent, HookManager, HookResult
from src.lib.smolagents.hooks.types import HookContext


def _slow_hook(context: HookContext) -> HookResult:
    """Hook that sleeps longer than any reasonable timeout."""
    time.sleep(30)
    return HookResult(success=True, decision="allow")


def _infinite_hook(context: HookContext) -> HookResult:
    """Hook with an infinite loop."""
    while True:
        time.sleep(0.01)


def _fast_hook(context: HookContext) -> HookResult:
    """Hook that completes immediately."""
    return HookResult(success=True, decision="allow", reason="fast")


class TestHookTimeout(unittest.TestCase):
    """Hook timeout enforcement tests."""

    def setUp(self):
        self.manager = HookManager()

    # --- Normal path ---

    def test_slow_hook_returns_timeout_reason(self):
        """A hook exceeding its timeout should return a result with a
        non-empty reason string mentioning timeout."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _slow_hook, timeout=0.5,
        )
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        # The slow hook was killed, so the final result should still allow
        # (timeout = non-blocking) but the reason should mention timeout.
        self.assertIn("timed out", (result.reason or "").lower())

    def test_fast_hook_not_affected_by_timeout(self):
        """A hook completing within its timeout should succeed normally."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _fast_hook, timeout=5.0,
        )
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.reason, "fast")

    # --- Error path ---

    def test_infinite_hook_is_terminated(self):
        """A hook with an infinite loop should be killed by timeout
        without blocking the manager indefinitely."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _infinite_hook, timeout=0.3,
        )
        start = time.monotonic()
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        elapsed = time.monotonic() - start
        # Should complete within a reasonable margin of the timeout
        self.assertLess(elapsed, 3.0, "Hook execution did not respect timeout")
        self.assertIn("timed out", (result.reason or "").lower())

    # --- Boundary ---

    def test_very_short_timeout(self):
        """Extremely short timeout should still produce a clean result."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _slow_hook, timeout=0.001,
        )
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        self.assertIsNotNone(result.reason)
        self.assertIn("timed out", result.reason.lower())

    def test_timeout_uses_daemon_worker_thread(self):
        """Timed-out Python hooks should run in daemon worker threads."""

        class RecordingThread(threading.Thread):
            created_daemon_flags = []
            created_names = []

            def __init__(self, *args, **kwargs):
                type(self).created_daemon_flags.append(kwargs.get("daemon"))
                type(self).created_names.append(kwargs.get("name") or "")
                super().__init__(*args, **kwargs)

        with patch(
            "src.lib.smolagents.hooks.hook_manager.Thread",
            RecordingThread,
        ):
            self.manager.register_hook(
                HookEvent.PRE_TOOL_USE, "*", _slow_hook, timeout=0.001,
            )
            result = self.manager.trigger_hooks(
                HookEvent.PRE_TOOL_USE, "test_tool", {},
            )

        self.assertIn("timed out", (result.reason or "").lower())
        self.assertTrue(RecordingThread.created_daemon_flags)
        self.assertTrue(all(RecordingThread.created_daemon_flags))
        self.assertTrue(
            all(name.startswith("hook-timeout-") for name in RecordingThread.created_names),
        )


if __name__ == "__main__":
    unittest.main()
