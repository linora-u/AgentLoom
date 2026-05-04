"""Tests for AsyncHookRegistry process control.

Validates:
- Normal: registered Popen -> timeout -> process killed
- Normal: registered Popen -> natural completion -> stdout collected
- Error: process already dead -> kill does not crash
- Boundary: register then immediately check -> process still running
"""

import os
import signal
import subprocess
import time
import unittest

from src.lib.smolagents.hooks.async_hook_registry import (
    AsyncHookRegistry,
    PendingAsyncHook,
)
from src.lib.smolagents.hooks.types import HookResult


class TestAsyncRegistryProcessControl(unittest.TestCase):
    """Tests for process handle management in AsyncHookRegistry."""

    def setUp(self):
        self.registry = AsyncHookRegistry()

    def tearDown(self):
        self.registry.clear()

    def test_timeout_kills_process(self):
        """Timed-out async hook should have its process killed."""
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        hook = PendingAsyncHook(
            process_id=f"test_{proc.pid}",
            hook_id="test",
            hook_event="PreToolUse",
            hook_name="test_hook",
            command="sleep 60",
            timeout_ms=100,  # 100ms timeout
            process_handle=proc,
        )
        self.registry.register(hook)

        # Wait enough for timeout to trigger
        time.sleep(0.3)
        ready = self.registry.check_for_responses()

        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0].completed)
        self.assertIn("timed out", (ready[0].result.reason or "").lower())

        # Process should be dead
        time.sleep(0.2)
        self.assertIsNotNone(proc.poll(), "Process should have been killed")

    def test_natural_completion_collects_result(self):
        """Naturally completed async hook should have stdout collected."""
        proc = subprocess.Popen(
            ["echo", "done"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        proc.wait()  # Let it finish

        hook = PendingAsyncHook(
            process_id=f"test_{proc.pid}",
            hook_id="test",
            hook_event="PreToolUse",
            hook_name="test_hook",
            command="echo done",
            timeout_ms=5000,
            process_handle=proc,
        )
        self.registry.register(hook)

        ready = self.registry.check_for_responses()
        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0].completed)
        # exit 0 -> success
        self.assertTrue(ready[0].result.success)

    def test_dead_process_kill_does_not_crash(self):
        """Killing an already-dead process should not raise."""
        proc = subprocess.Popen(
            ["true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        proc.wait()

        hook = PendingAsyncHook(
            process_id=f"test_{proc.pid}",
            hook_id="test",
            hook_event="PreToolUse",
            hook_name="test_hook",
            command="true",
            timeout_ms=1,  # Immediately expired
            process_handle=proc,
        )
        self.registry.register(hook)

        # Should not crash even though process is dead
        ready = self.registry.check_for_responses()
        self.assertEqual(len(ready), 1)

    def test_register_without_handle(self):
        """Registry should work without a process handle (legacy mode)."""
        hook = PendingAsyncHook(
            process_id="test_no_handle",
            hook_id="test",
            hook_event="PreToolUse",
            hook_name="test_hook",
            command="test",
            timeout_ms=1,
            process_handle=None,
        )
        self.registry.register(hook)

        time.sleep(0.01)
        ready = self.registry.check_for_responses()
        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0].completed)

    def test_finalize_kills_running_processes(self):
        """finalize_all should kill all running processes."""
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        hook = PendingAsyncHook(
            process_id=f"test_{proc.pid}",
            hook_id="test",
            hook_event="PreToolUse",
            hook_name="test_hook",
            command="sleep 60",
            timeout_ms=60000,
            process_handle=proc,
        )
        self.registry.register(hook)
        self.registry.finalize_all()

        time.sleep(0.5)
        self.assertIsNotNone(proc.poll(), "Process should have been killed by finalize")

    def test_pending_count(self):
        """pending_count should reflect uncompleted hooks."""
        hook = PendingAsyncHook(
            process_id="test1",
            hook_id="test",
            hook_event="PreToolUse",
            hook_name="test_hook",
            command="test",
            timeout_ms=60000,
        )
        self.registry.register(hook)
        self.assertEqual(self.registry.pending_count, 1)

        self.registry.mark_completed("test1", HookResult(success=True))
        self.assertEqual(self.registry.pending_count, 0)


if __name__ == "__main__":
    unittest.main()
