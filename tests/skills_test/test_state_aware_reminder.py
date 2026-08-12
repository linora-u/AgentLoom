"""Tests for the state-aware reminder engine (S2).

Validates:
- Template detection: empty file, template content, real content
- Step number reading from the versioned Hook stdin contract
- Write tracker: mtime change detection, staleness growth, reset on write
- PostToolUse behavior: grace period, gentle/urgent thresholds, cooldown
- Re-remind after stale: write once then stop → gets reminded again
"""

import json
import sys
import time
import unittest
from pathlib import Path

# Add the standalone Hook Bundle scripts directory to sys.path.
_SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent.parent
    / "hooks" / "agent-recall-with-files" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import common as recall_common  # noqa: E402
from common import (  # noqa: E402
    CONTEXT_FILE,
    INSIGHTS_FILE,
    TEMPLATE_SIGNATURES,
    TRACE_FILE,
    detect_writes_and_update,
    get_step_number,
    is_template_only,
    load_write_tracker,
    save_write_tracker,
    summarize_insights,
)


class TestIsTemplateOnly(unittest.TestCase):
    """Tests for is_template_only()."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_is_template(self):
        """Non-existent file should be considered template."""
        self.assertTrue(is_template_only(self.tmpdir / "nope.md", TRACE_FILE))

    def test_empty_file_is_template(self):
        """Empty file should be considered template."""
        p = self.tmpdir / TRACE_FILE
        p.write_text("", encoding="utf-8")
        self.assertTrue(is_template_only(p, TRACE_FILE))

    def test_whitespace_only_is_template(self):
        """Whitespace-only file should be considered template."""
        p = self.tmpdir / TRACE_FILE
        p.write_text("  \n\n  ", encoding="utf-8")
        self.assertTrue(is_template_only(p, TRACE_FILE))

    def test_template_content_detected(self):
        """File with known template signature should be detected."""
        p = self.tmpdir / TRACE_FILE
        sig = TEMPLATE_SIGNATURES[TRACE_FILE]
        p.write_text(f"# Trace\n\n## Log\n- {sig}\n", encoding="utf-8")
        self.assertTrue(is_template_only(p, TRACE_FILE))

    def test_context_template_detected(self):
        """context.md template content detected."""
        p = self.tmpdir / CONTEXT_FILE
        sig = TEMPLATE_SIGNATURES[CONTEXT_FILE]
        p.write_text(f"# Context\n\n## Goal\n\n{sig}\n", encoding="utf-8")
        self.assertTrue(is_template_only(p, CONTEXT_FILE))

    def test_real_content_not_template(self):
        """File with real content should NOT be detected as template."""
        p = self.tmpdir / TRACE_FILE
        p.write_text(
            "# Trace\n\n## Log\n"
            "- [2024-06-20 10:00:00] Scanned 5 files for shared variables.\n"
            "- [2024-06-20 10:01:00] Found 3 potential race conditions.\n"
            "- [2024-06-20 10:02:00] Analyzing lock patterns.\n"
            + "More content here\n" * 50,  # make it > 500 chars
            encoding="utf-8",
        )
        self.assertFalse(is_template_only(p, TRACE_FILE))

    def test_short_real_content_without_signature(self):
        """Short file without template signature should not be template."""
        p = self.tmpdir / TRACE_FILE
        p.write_text("# Trace\n\nActual log entry here.", encoding="utf-8")
        self.assertFalse(is_template_only(p, TRACE_FILE))

    def test_unknown_file_type_not_template(self):
        """Unknown file_type with no signature should not match."""
        p = self.tmpdir / "random.md"
        p.write_text("Some content here", encoding="utf-8")
        self.assertFalse(is_template_only(p, "random.md"))


class TestGetStepNumber(unittest.TestCase):
    """Tests for get_step_number()."""

    def tearDown(self):
        recall_common._set_hook_context_for_testing(None)

    def test_from_hook_stdin_payload(self):
        recall_common._set_hook_context_for_testing({"schema_version": 1, "step_number": 12})
        self.assertEqual(get_step_number(), 12)

    def test_fallback_zero(self):
        """Should return zero when lifecycle payload has no step."""
        recall_common._set_hook_context_for_testing({"schema_version": 1})
        self.assertEqual(get_step_number(), 0)

    def test_invalid_step_type_falls_back_zero(self):
        recall_common._set_hook_context_for_testing({"schema_version": 1, "step_number": "3"})
        self.assertEqual(get_step_number(), 0)


class TestWriteTracker(unittest.TestCase):
    """Tests for write tracker persistence and freshness detection."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        # Create runtime-like directory with template files.
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        for fname in [TRACE_FILE, CONTEXT_FILE, INSIGHTS_FILE]:
            sig = TEMPLATE_SIGNATURES.get(fname, "")
            (self.tmpdir / fname).write_text(f"# Header\n{sig}\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_default_tracker(self):
        """First load should return default tracker with zero values."""
        tracker = load_write_tracker(self.tmpdir)
        self.assertEqual(tracker["last_reminded_at_step"], 0)
        self.assertEqual(tracker[TRACE_FILE]["last_mtime"], 0)

    def test_save_and_reload(self):
        """Tracker should be persisted and reloadable."""
        tracker = load_write_tracker(self.tmpdir)
        tracker["last_reminded_at_step"] = 5
        save_write_tracker(self.tmpdir, tracker)
        reloaded = load_write_tracker(self.tmpdir)
        self.assertEqual(reloaded["last_reminded_at_step"], 5)

    def test_template_files_detected_as_never_written(self):
        """Template-only files should have stale_steps = -1."""
        tracker = load_write_tracker(self.tmpdir)
        staleness = detect_writes_and_update(
            self.tmpdir, tracker, step=5, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )
        self.assertEqual(staleness[TRACE_FILE], -1)
        self.assertEqual(staleness[CONTEXT_FILE], -1)

    def test_mtime_change_detected(self):
        """Writing real content should be detected via mtime change."""
        tracker = load_write_tracker(self.tmpdir)
        # First check at step 1 — template only.
        detect_writes_and_update(
            self.tmpdir, tracker, step=1, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )

        # Write real content to trace.md.
        time.sleep(0.05)  # Ensure mtime difference.
        (self.tmpdir / TRACE_FILE).write_text(
            "# Trace\n\n## Log\n- Real entry here\n" + "x" * 500,
            encoding="utf-8",
        )
        staleness = detect_writes_and_update(
            self.tmpdir, tracker, step=3, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )
        self.assertEqual(staleness[TRACE_FILE], 0)  # Fresh.

    def test_staleness_grows_over_steps(self):
        """Stale_steps should increase when file is not modified."""
        tracker = load_write_tracker(self.tmpdir)
        # Write real content at step 2.
        time.sleep(0.05)
        (self.tmpdir / TRACE_FILE).write_text(
            "# Trace\n\n## Log\n- Entry\n" + "x" * 500,
            encoding="utf-8",
        )
        detect_writes_and_update(
            self.tmpdir, tracker, step=2, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )
        self.assertEqual(tracker[TRACE_FILE]["last_written_at_step"], 2)

        # Check at step 6 without modifying.
        staleness = detect_writes_and_update(
            self.tmpdir, tracker, step=6, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )
        self.assertEqual(staleness[TRACE_FILE], 4)  # 6 - 2 = 4.

    def test_staleness_resets_on_new_write(self):
        """Writing again should reset staleness to 0."""
        tracker = load_write_tracker(self.tmpdir)
        # Initial write at step 2.
        time.sleep(0.05)
        (self.tmpdir / TRACE_FILE).write_text("# Trace\nEntry1\n" + "x" * 500, encoding="utf-8")
        detect_writes_and_update(
            self.tmpdir, tracker, step=2, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )

        # Stale at step 8.
        staleness = detect_writes_and_update(
            self.tmpdir, tracker, step=8, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )
        self.assertEqual(staleness[TRACE_FILE], 6)

        # Write again at step 9.
        time.sleep(0.05)
        (self.tmpdir / TRACE_FILE).write_text("# Trace\nEntry2\n" + "x" * 500, encoding="utf-8")
        staleness = detect_writes_and_update(
            self.tmpdir, tracker, step=9, persistent_insights=self.tmpdir / INSIGHTS_FILE
        )
        self.assertEqual(staleness[TRACE_FILE], 0)  # Fresh again.


class TestInsightCompaction(unittest.TestCase):
    def test_old_entries_are_replaced_by_bounded_tag_summary(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INSIGHTS_FILE
            entries = [
                f"- [2026-08-12] [fact] Durable fact {index}."
                for index in range(100)
            ]
            path.write_text(
                "# Insights\n\n## Log\n" + "\n".join(entries) + "\n",
                encoding="utf-8",
            )

            summarize_insights(path)

            compacted = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(compacted.splitlines()), 80)
            self.assertIn("Older entries compacted: 70 total; fact=70.", compacted)
            self.assertNotIn("Durable fact 0.", compacted)
            self.assertIn("Durable fact 99.", compacted)


class TestPostToolUseBehavior(unittest.TestCase):
    """Integration tests for on_post_tool_use.py freshness-driven behavior.

    These tests import and call main() from on_post_tool_use.py, mocking
    the environment and capturing stdout output.
    """

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        # Create template files.
        for fname in [TRACE_FILE, CONTEXT_FILE, INSIGHTS_FILE]:
            sig = TEMPLATE_SIGNATURES.get(fname, "")
            (self.tmpdir / fname).write_text(f"# Header\n{sig}\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_post_hook(self, step: int) -> dict:
        """Run on_post_tool_use.main() and return the JSON output."""
        import io
        from contextlib import redirect_stdout

        recall_common._set_hook_context_for_testing(
            {
                "schema_version": 1,
                "hook_event_name": "PostToolUse",
                "agent_name": "test_agent",
                "runtime_agent_path": "test_agent",
                "step_number": step,
                "project_root": str(self.tmpdir.parent),
                "agent_task_workspace": str(self.tmpdir),
                "agent_insights_path": str(self.tmpdir / INSIGHTS_FILE),
                "agent_visualization_path": str(self.tmpdir / "visualization.json"),
                "tool_name": "test_tool",
                "tool_input": {},
            }
        )
        if "on_post_tool_use" in sys.modules:
            del sys.modules["on_post_tool_use"]
        import on_post_tool_use
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                on_post_tool_use.main()
            raw = buf.getvalue().strip()
            return json.loads(raw) if raw else {}
        finally:
            recall_common._set_hook_context_for_testing(None)

    def test_grace_period_silent(self):
        """Steps 1-3 should produce no reminder (grace period)."""
        for step in [1, 2, 3]:
            result = self._run_post_hook(step)
            self.assertEqual(result.get("decision"), "allow")
            self.assertNotIn("agent_context", result, f"Step {step} should be silent")

    def test_empty_template_triggers_urgent(self):
        """After grace period, empty templates should trigger urgent."""
        result = self._run_post_hook(4)
        self.assertIn("agent_context", result)
        self.assertIn("never written", result["agent_context"])

    def test_fresh_files_silent(self):
        """Files written this step should not trigger reminder."""
        # Write real content.
        time.sleep(0.05)
        for fname in [TRACE_FILE, CONTEXT_FILE]:
            (self.tmpdir / fname).write_text("Real content\n" + "x" * 500, encoding="utf-8")
        # Run at step 5 — should detect fresh writes.
        result = self._run_post_hook(5)
        self.assertEqual(result.get("decision"), "allow")
        # No urgent/gentle since files are fresh.
        ctx = result.get("agent_context", "")
        self.assertNotIn("WARNING", ctx)

    def test_reminder_cooldown(self):
        """Should not remind again within TURNS_BETWEEN_REMINDERS steps."""
        # Step 4: triggers reminder (templates empty).
        result1 = self._run_post_hook(4)
        self.assertIn("agent_context", result1)
        # Step 5: should be silent (cooldown).
        result2 = self._run_post_hook(5)
        self.assertNotIn("agent_context", result2)
        # Step 6: should be silent (cooldown, 6-4=2 < 3).
        result3 = self._run_post_hook(6)
        self.assertNotIn("agent_context", result3)

    def test_re_reminds_after_stale(self):
        """Should remind again when a previously-written file becomes stale."""
        # Write real content to make it "written at step 4".
        time.sleep(0.05)
        for fname in [TRACE_FILE, CONTEXT_FILE]:
            (self.tmpdir / fname).write_text("Real content\n" + "x" * 500, encoding="utf-8")

        # Step 4: detect fresh write.
        self._run_post_hook(4)
        # Step 8: stale_steps = 8-4 = 4 >= gentle_after(4) for trace.
        result = self._run_post_hook(8)
        ctx = result.get("agent_context", "")
        # Should see a gentle reminder for trace.md.
        self.assertIn(TRACE_FILE, ctx)
        self.assertIn("steps ago", ctx)


if __name__ == "__main__":
    unittest.main()
