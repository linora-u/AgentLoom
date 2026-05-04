"""Tests for the stall watchdog — interactive prompt detection."""

import os
import tempfile
import time

import pytest

from src.tools.shell.stall_watchdog import StallWatchdog, PROMPT_PATTERNS


class TestPromptPatterns:
    """Verify the prompt detection patterns match expected inputs."""

    @pytest.mark.parametrize("line", [
        "(y/n)",
        "[y/n]",
        "(yes/no)",
        "Do you want to continue? ",
        "Would you like to proceed?",
        "Are you sure? ",
        "Press Enter to continue",
        "Press any key",
        "Continue?",
        "Overwrite?",
        "Proceed?",
        "[Y/n]",
        "[yes/No]",
    ])
    def test_matches_known_prompts(self, line):
        assert any(p.search(line) for p in PROMPT_PATTERNS), (
            f"Pattern should match: {line!r}"
        )

    @pytest.mark.parametrize("line", [
        "Building module 3/10...",
        "Compiling source.c",
        "100% complete",
        "WARNING: something happened",
        "ERROR: build failed",
        "running tests...",
        "",
    ])
    def test_does_not_match_non_prompts(self, line):
        assert not any(p.search(line) for p in PROMPT_PATTERNS), (
            f"Pattern should NOT match: {line!r}"
        )


class TestStallWatchdog:
    """Integration tests for the StallWatchdog."""

    def test_detects_stall_with_prompt(self, tmp_path):
        """Output stops growing and last line is a prompt."""
        out = tmp_path / "output.txt"
        out.write_text("Building...\nDo you want to continue? ")

        sw = StallWatchdog(
            task_id="t1",
            output_path=str(out),
            poll_interval=0.3,
            stall_threshold=1.0,  # Speed up for testing.
        )
        sw.start()

        # Wait for detection.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sw.stall_message is not None:
                break
            time.sleep(0.2)

        sw.stop()
        assert sw.stall_message is not None
        assert "interactive input" in sw.stall_message

    def test_no_false_positive_without_prompt(self, tmp_path):
        """Output stops growing but last line is NOT a prompt."""
        out = tmp_path / "output.txt"
        out.write_text("Compiling module 5/10...\n")

        sw = StallWatchdog(
            task_id="t2",
            output_path=str(out),
            poll_interval=0.3,
            stall_threshold=1.0,
        )
        sw.start()

        # Wait enough for the stall threshold.
        time.sleep(2.5)
        sw.stop()

        # Should NOT detect a stall because the last line is not a prompt.
        assert sw.stall_message is None

    def test_no_stall_when_output_growing(self, tmp_path):
        """Output keeps growing — no stall detected."""
        out = tmp_path / "output.txt"
        out.write_text("line 1\n")

        sw = StallWatchdog(
            task_id="t3",
            output_path=str(out),
            poll_interval=0.3,
            stall_threshold=1.0,
        )
        sw.start()

        # Keep writing output.
        for i in range(5):
            time.sleep(0.5)
            with open(str(out), "a") as f:
                f.write(f"line {i + 2}\n")

        sw.stop()
        assert sw.stall_message is None

    def test_missing_output_file(self, tmp_path):
        """Output file doesn't exist — should not crash."""
        sw = StallWatchdog(
            task_id="t4",
            output_path=str(tmp_path / "nonexistent.txt"),
            poll_interval=0.3,
            stall_threshold=1.0,
        )
        sw.start()
        time.sleep(1.5)
        sw.stop()
        assert sw.stall_message is None

    def test_empty_output_file(self, tmp_path):
        """Output file exists but is empty."""
        out = tmp_path / "output.txt"
        out.write_text("")

        sw = StallWatchdog(
            task_id="t5",
            output_path=str(out),
            poll_interval=0.3,
            stall_threshold=1.0,
        )
        sw.start()
        time.sleep(2)
        sw.stop()
        # Empty file — no prompt to detect.
        assert sw.stall_message is None

    def test_stop_is_idempotent(self, tmp_path):
        out = tmp_path / "output.txt"
        out.write_text("test\n")

        sw = StallWatchdog(
            task_id="t6",
            output_path=str(out),
            poll_interval=0.3,
            stall_threshold=1.0,
        )
        sw.start()
        sw.stop()
        sw.stop()  # Should not raise.

    def test_one_shot_detection(self, tmp_path):
        """After detecting a stall, the watchdog should stop polling."""
        out = tmp_path / "output.txt"
        out.write_text("Continue? ")

        sw = StallWatchdog(
            task_id="t7",
            output_path=str(out),
            poll_interval=0.3,
            stall_threshold=0.5,
        )
        sw.start()

        # Wait for detection.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sw.stall_message is not None:
                break
            time.sleep(0.2)

        assert sw.stall_message is not None
        # The internal _stopped flag should be True.
        assert sw._stopped is True
