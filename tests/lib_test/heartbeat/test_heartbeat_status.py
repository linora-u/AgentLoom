"""Tests for ``src.lib.heartbeat.status`` — crash detection functions."""

from __future__ import annotations

import os
import time

import pytest

from src.lib.heartbeat.status import (
    HEARTBEAT_STALE_THRESHOLD,
    detect_crashed_status,
    detect_worker_call_crashed,
)


class TestDetectCrashedStatus:
    """Tests for supervisor crash detection."""

    def test_none_heartbeat_is_crashed(self):
        assert detect_crashed_status(None) == "crashed"

    def test_stopped_heartbeat_is_crashed(self):
        hb = {"status": "stopped", "pid": os.getpid(), "timestamp": time.time()}
        assert detect_crashed_status(hb) == "crashed"

    def test_exited_heartbeat_is_crashed(self):
        hb = {"status": "exited", "pid": os.getpid(), "timestamp": time.time()}
        assert detect_crashed_status(hb) == "crashed"

    def test_dead_pid_is_crashed(self):
        hb = {"status": "running", "pid": 99999999, "timestamp": time.time()}
        assert detect_crashed_status(hb) == "crashed"

    def test_stale_timestamp_is_crashed(self):
        hb = {
            "status": "running",
            "pid": os.getpid(),
            "timestamp": time.time() - HEARTBEAT_STALE_THRESHOLD - 10,
        }
        assert detect_crashed_status(hb) == "crashed"

    def test_fresh_running_is_running(self):
        hb = {"status": "running", "pid": os.getpid(), "timestamp": time.time()}
        assert detect_crashed_status(hb) == "running"


class TestDetectWorkerCallCrashed:
    """Tests for per-worker-call crash detection."""

    def test_none_heartbeat_is_unknown(self):
        assert detect_worker_call_crashed(None, 0) == "unknown"

    def test_missing_call_is_unknown(self):
        hb = {"timestamp": time.time(), "calls": {}}
        assert detect_worker_call_crashed(hb, 0) == "unknown"

    def test_completed_call_stays_completed(self):
        hb = {"timestamp": time.time(), "calls": {"0": {"status": "completed"}}}
        assert detect_worker_call_crashed(hb, 0) == "completed"

    def test_failed_call_stays_failed(self):
        hb = {"timestamp": time.time(), "calls": {"0": {"status": "failed"}}}
        assert detect_worker_call_crashed(hb, 0) == "failed"

    def test_running_with_fresh_heartbeat_is_running(self):
        hb = {"timestamp": time.time(), "calls": {"0": {"status": "running"}}}
        assert detect_worker_call_crashed(hb, 0) == "running"

    def test_running_with_stale_heartbeat_is_crashed(self):
        hb = {
            "timestamp": time.time() - HEARTBEAT_STALE_THRESHOLD - 10,
            "calls": {"0": {"status": "running"}},
        }
        assert detect_worker_call_crashed(hb, 0) == "crashed"

    def test_call_index_as_int_and_str(self):
        """call_index can be passed as int or str."""
        hb = {"timestamp": time.time(), "calls": {"2": {"status": "completed"}}}
        assert detect_worker_call_crashed(hb, 2) == "completed"
        assert detect_worker_call_crashed(hb, "2") == "completed"
