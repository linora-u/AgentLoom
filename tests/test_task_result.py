"""
Tests for src.lib.concurrency.models.TaskResult dataclass.
"""

from __future__ import annotations

from src.lib.concurrency.models import TaskResult


class TestTaskResult:
    def test_completed_result(self):
        r = TaskResult(task_id="dir1", status="completed", result="analysis text")
        assert r.status == "completed"
        assert r.result == "analysis text"
        assert r.error is None
        assert r.error_trace is None

    def test_failed_result(self):
        r = TaskResult(task_id="dir2", status="failed", error="boom", error_trace="Traceback...")
        assert r.status == "failed"
        assert r.error == "boom"
        assert r.result is None

    def test_skipped_result(self):
        r = TaskResult(task_id="dir3", status="skipped", error="circuit breaker")
        assert r.status == "skipped"

    def test_default_values(self):
        r = TaskResult(task_id="x", status="completed")
        assert r.result is None
        assert r.error is None
        assert r.error_trace is None
        assert r.duration_seconds == 0.0

    def test_duration_float(self):
        r = TaskResult(task_id="x", status="completed", duration_seconds=1.5)
        assert isinstance(r.duration_seconds, float)
        assert r.duration_seconds == 1.5
