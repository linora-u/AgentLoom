"""Tests for ``src.lib.heartbeat.HeartbeatWriter``."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.lib.heartbeat import HeartbeatWriter
from src.lib.runtime import RuntimeHome


@pytest.fixture()
def hb_path(tmp_path: Path) -> Path:
    return tmp_path / "heartbeat.json"


class TestHeartbeatWriter:

    def test_records_current_run_id(self, hb_path: Path):
        hb = HeartbeatWriter(
            path=hb_path,
            agent_name="test_agent",
            run_id="run_current",
            interval=0.2,
        )
        hb.start()
        time.sleep(0.3)
        hb.stop()

        assert json.loads(hb_path.read_text())["run_id"] == "run_current"

    def test_writes_file(self, hb_path: Path):
        hb = HeartbeatWriter(path=hb_path, agent_name="test_agent", run_id="run_test", interval=0.2)
        hb.start()
        time.sleep(0.5)
        hb.stop()
        assert hb_path.exists()
        data = json.loads(hb_path.read_text())
        assert data["agent_name"] == "test_agent"
        assert "pid" in data
        assert "timestamp" in data

    def test_updates_periodically(self, hb_path: Path):
        hb = HeartbeatWriter(path=hb_path, agent_name="test_agent", run_id="run_test", interval=0.2)
        hb.start()
        time.sleep(0.3)
        ts1 = json.loads(hb_path.read_text())["timestamp"]
        time.sleep(0.3)
        ts2 = json.loads(hb_path.read_text())["timestamp"]
        hb.stop()
        assert ts2 > ts1

    def test_stop_writes_stopped(self, hb_path: Path):
        hb = HeartbeatWriter(path=hb_path, agent_name="test_agent", run_id="run_test", interval=0.2)
        hb.start()
        time.sleep(0.3)
        hb.stop()
        data = json.loads(hb_path.read_text())
        assert data["status"] == "stopped"

    def test_update_step(self, hb_path: Path):
        hb = HeartbeatWriter(path=hb_path, agent_name="test_agent", run_id="run_test", interval=0.2)
        hb.start()
        hb.update_step(5)
        time.sleep(0.4)
        hb.stop()
        data = json.loads(hb_path.read_text())
        assert data["step"] == 5

    def test_daemon_thread(self, hb_path: Path):
        hb = HeartbeatWriter(path=hb_path, agent_name="test_agent", run_id="run_test", interval=0.2)
        hb.start()
        assert hb._thread is not None
        assert hb._thread.daemon is True
        hb.stop()

    def test_writer_stays_on_original_task_inode_after_path_replacement(
        self,
        tmp_path: Path,
    ) -> None:
        home = RuntimeHome(tmp_path / ".agentloom")
        first = home.context(application_id="app", task_id="first", run_id="run-a")
        second = home.context(application_id="app", task_id="second", run_id="run-b")
        first.prepare_checkpoint()
        second.prepare_checkpoint()
        second.heartbeat_path.write_text('{"agent_name":"SECOND"}', encoding="utf-8")
        heartbeat = HeartbeatWriter(
            path=first.heartbeat_path,
            agent_name="FIRST",
            run_id="run-a",
        )
        detached = first.checkpoint_dir.parent / "first-detached"
        first.checkpoint_dir.rename(detached)
        first.checkpoint_dir.symlink_to(second.checkpoint_dir, target_is_directory=True)

        heartbeat._write_once()

        assert json.loads(second.heartbeat_path.read_text())["agent_name"] == "SECOND"
        assert json.loads((detached / "heartbeat.json").read_text())["agent_name"] == "FIRST"
