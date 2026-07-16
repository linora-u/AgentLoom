"""Tests for ``src.lib.heartbeat.WorkerHeartbeat``."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.lib.heartbeat import WorkerHeartbeat


@pytest.fixture()
def hb_path(tmp_path: Path) -> Path:
    return tmp_path / "workers" / "scan_code" / "heartbeat.json"


class TestWorkerHeartbeat:

    def test_records_current_run_id(self, hb_path: Path):
        heartbeat = WorkerHeartbeat(
            path=hb_path,
            agent_name="scan_code",
            run_id="run_current",
            interval=0.2,
        )
        heartbeat.register_call(0)
        heartbeat.start()
        time.sleep(0.3)
        heartbeat.stop()

        assert json.loads(hb_path.read_text())["run_id"] == "run_current"

    def test_register_and_write(self, hb_path: Path):
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.2)
        whb.register_call(0)
        whb.start()
        time.sleep(0.5)
        whb.stop()
        assert hb_path.exists()
        data = json.loads(hb_path.read_text())
        assert data["agent_name"] == "scan_code"
        assert "0" in data["calls"]
        assert data["calls"]["0"]["status"] in ("running", "stopped")

    def test_multiple_calls(self, hb_path: Path):
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.2)
        whb.register_call(0)
        whb.register_call(1)
        whb.start()
        time.sleep(0.4)
        whb.update_call_status(0, "completed")
        time.sleep(0.3)
        whb.stop()
        data = json.loads(hb_path.read_text())
        calls = data["calls"]
        assert "0" in calls and "1" in calls
        assert calls["0"]["status"] == "completed"
        assert "finished_at" in calls["0"]

    def test_update_step(self, hb_path: Path):
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.2)
        whb.register_call(0)
        whb.update_call_step(0, 5)
        whb.start()
        time.sleep(0.4)
        whb.stop()
        data = json.loads(hb_path.read_text())
        assert data["calls"]["0"]["step"] == 5

    def test_all_calls_terminal(self, hb_path: Path):
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.2)
        whb.register_call(0)
        whb.register_call(1)
        assert not whb.all_calls_terminal()
        whb.update_call_status(0, "completed")
        assert not whb.all_calls_terminal()
        whb.update_call_status(1, "failed")
        assert whb.all_calls_terminal()

    def test_thread_safety(self, hb_path: Path):
        """Multiple threads register and update calls concurrently."""
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.1)
        whb.start()

        errors: list[Exception] = []

        def worker(call_idx: int):
            try:
                whb.register_call(call_idx)
                for step in range(5):
                    whb.update_call_step(call_idx, step)
                    time.sleep(0.05)
                whb.update_call_status(call_idx, "completed")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        whb.stop()
        assert not errors, f"Thread errors: {errors}"
        data = json.loads(hb_path.read_text())
        assert len(data["calls"]) == 8
        for ci in range(8):
            assert data["calls"][str(ci)]["status"] == "completed"

    def test_daemon_thread(self, hb_path: Path):
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.2)
        whb.start()
        assert whb._thread is not None
        assert whb._thread.daemon is True
        whb.stop()

    def test_on_stopping_marks_running_as_stopped(self, hb_path: Path):
        whb = WorkerHeartbeat(path=hb_path, agent_name="scan_code", run_id="run_test", interval=0.2)
        whb.register_call(0)
        whb.register_call(1)
        whb.update_call_status(0, "completed")
        # call 1 is still "running"
        whb.start()
        time.sleep(0.3)
        whb.stop()
        data = json.loads(hb_path.read_text())
        assert data["calls"]["0"]["status"] == "completed"
        assert data["calls"]["1"]["status"] == "stopped"
