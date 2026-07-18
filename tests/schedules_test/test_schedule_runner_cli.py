from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from click.testing import CliRunner

from src.__main__ import main
from src.schedules.runner import ScheduleRunner
from src.schedules.schedule import once_schedule
from src.schedules.service import ScheduleService
from src.schedules.store import ScheduleStore

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def _job(store: ScheduleStore, tmp_path: Path) -> dict:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    return store.add_job(
        name="test",
        yaml_path=yaml_path,
        schedule=once_schedule("2026-07-19T08:00:00Z"),
        now=NOW,
    )


def test_runner_uses_canonical_agentloom_command_by_default(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)

    assert ScheduleRunner(store).command_for(job) == [
        sys.executable,
        "-I",
        "-m",
        "src",
        "run",
        "agent.yaml",
    ]


def test_default_runner_isolated_mode_rejects_project_local_src_shadowing(tmp_path: Path) -> None:
    source_package = tmp_path / "src"
    source_package.mkdir()
    (source_package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "shadowed"
    (source_package / "__main__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)

    execution = ScheduleRunner(store, poll_seconds=0.01).run_now(job["id"], now=NOW)

    assert execution["status"] == "failed"
    assert not marker.exists()


def test_run_now_persists_complete_output_exit_and_error(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)
    command = [
        sys.executable,
        "-c",
        "import sys; print('hello stdout'); print('bad stderr', file=sys.stderr); sys.exit(7)",
    ]
    runner = ScheduleRunner(store, command_factory=lambda _job: command, poll_seconds=0.01)

    execution = runner.run_now(job["id"], now=NOW)

    assert execution["status"] == "failed"
    assert execution["exit_code"] == 7
    assert execution["error"] == "process exited with status 7"
    assert (tmp_path / execution["stdout_path"]).read_text(encoding="utf-8").strip() == "hello stdout"
    assert (tmp_path / execution["stderr_path"]).read_text(encoding="utf-8").strip() == "bad stderr"


def test_runner_rejects_symlinked_execution_log_target(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)
    claim = store.claim_now(job["id"], owner="test", now=NOW)
    execution_id = claim["execution"]["id"]
    store.executions_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.log"
    outside.write_text("sentinel\n", encoding="utf-8")
    (store.executions_dir / f"{execution_id}.stdout.log").symlink_to(outside)
    runner = ScheduleRunner(
        store,
        command_factory=lambda _job: [sys.executable, "-c", "print('escaped')"],
        poll_seconds=0.01,
    )

    execution = runner.execute_claim(claim)

    assert execution["status"] == "failed"
    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_runner_outputs_remain_anchored_when_agentloom_path_is_swapped(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)
    claim = store.claim_now(job["id"], owner="test", now=NOW)
    anchored_agentloom = tmp_path / ".agentloom-anchored"
    (tmp_path / ".agentloom").rename(anchored_agentloom)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agentloom").symlink_to(outside, target_is_directory=True)
    runner = ScheduleRunner(
        store,
        command_factory=lambda _job: [sys.executable, "-c", "print('anchored')"],
        poll_seconds=0.01,
    )

    execution = runner.execute_claim(claim)

    assert execution["status"] == "succeeded"
    assert (anchored_agentloom / "schedules/executions" / f"{execution['id']}.stdout.log").read_text(
        encoding="utf-8"
    ).strip() == "anchored"
    assert not (outside / "schedules/executions").exists()


def test_heartbeat_rejects_symlink_target_and_remains_anchored_after_path_swap(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path)
    service = ScheduleService(store)
    store.schedules_dir.mkdir(parents=True, exist_ok=True)
    outside_heartbeat = tmp_path / "outside-heartbeat.json"
    outside_heartbeat.write_text('{"sentinel": true}\n', encoding="utf-8")
    service.heartbeat_path.symlink_to(outside_heartbeat)

    with pytest.raises((OSError, RuntimeError)):
        service._write_heartbeat(force=True)
    assert json.loads(outside_heartbeat.read_text(encoding="utf-8")) == {"sentinel": True}

    service.heartbeat_path.unlink()
    anchored_agentloom = tmp_path / ".agentloom-anchored"
    (tmp_path / ".agentloom").rename(anchored_agentloom)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agentloom").symlink_to(outside, target_is_directory=True)

    service._write_heartbeat(force=True)

    assert (anchored_agentloom / "schedules/serve-status.json").is_file()
    assert not (outside / "schedules/serve-status.json").exists()


def test_service_rejects_symlinked_server_lock_target(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    service = ScheduleService(store)
    outside_lock = tmp_path / "outside-serve.lock"
    outside_lock.write_text("sentinel", encoding="utf-8")
    service.server_lock_path.symlink_to(outside_lock)

    with pytest.raises((OSError, RuntimeError)):
        service.serve(tick_seconds=0.1, max_ticks=1)

    assert outside_lock.read_text(encoding="utf-8") == "sentinel"


def test_run_now_keyboard_interrupt_still_finishes_the_claim(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    runner = ScheduleRunner(store, command_factory=lambda _job: command, poll_seconds=0.01)
    interrupted = False

    def interrupt_poll() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    execution = runner.run_now(job["id"], now=NOW, progress=interrupt_poll)

    assert execution["status"] == "failed"
    assert execution["error"] == "execution interrupted"
    assert store.get_job(job["id"])["claim"] is None
    assert not ScheduleService._pid_is_alive(execution["pid"])


def test_cli_registers_all_schedule_operations_and_can_add_list_pause_resume_remove(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    runner = CliRunner()
    prefix = ["schedules", "--project", str(tmp_path)]

    help_result = runner.invoke(main, ["schedules", "--help"])
    assert help_result.exit_code == 0
    for command in ("list", "add", "remove", "pause", "resume", "run", "serve", "status"):
        assert command in help_result.output

    added = runner.invoke(
        main,
        prefix
        + [
            "add",
            str(yaml_path),
            "--name",
            "hourly",
            "--every",
            "1h",
            "--timezone",
            "Asia/Shanghai",
            "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    job = json.loads(added.output)

    listed = runner.invoke(main, prefix + ["list", "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)[0]["id"] == job["id"]

    history = runner.invoke(main, prefix + ["list", "--history", "--json"])
    assert history.exit_code == 0
    assert json.loads(history.output) == []

    paused = runner.invoke(main, prefix + ["pause", job["id"], "--json"])
    assert paused.exit_code == 0
    assert json.loads(paused.output)["state"] == "paused"

    resumed = runner.invoke(main, prefix + ["resume", job["id"], "--json"])
    assert resumed.exit_code == 0
    assert json.loads(resumed.output)["state"] == "scheduled"

    status = runner.invoke(main, prefix + ["status", "--json"])
    assert status.exit_code == 0
    assert json.loads(status.output)["job_count"] == 1

    removed = runner.invoke(main, prefix + ["remove", job["id"]])
    assert removed.exit_code == 0
    assert ScheduleStore(tmp_path).list_jobs() == []


def test_one_tick_server_writes_a_stopped_health_record(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    service = ScheduleService(store)

    service.serve(tick_seconds=0.1, max_ticks=1)
    status = service.status()

    assert status["state"] == "stopped"
    assert status["heartbeat"]["last_success_at"] is not None
    assert status["heartbeat"]["stopped_at"] is not None


def test_persistent_server_reports_running_until_its_stop_event(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    service = ScheduleService(store)
    stop = Event()
    thread = Thread(
        target=service.serve,
        kwargs={"tick_seconds": 0.1, "stop_event": stop},
        daemon=True,
    )

    thread.start()
    for _ in range(40):
        if service.heartbeat_path.exists():
            break
        stop.wait(0.025)

    assert ScheduleService(store).status()["state"] == "running"
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert ScheduleService(store).status()["state"] == "stopped"


def test_stop_event_interrupts_the_running_due_agent_and_stops_claiming(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    due_at = datetime(2000, 1, 1, tzinfo=UTC)
    first = store.add_job(
        name="first",
        yaml_path=yaml_path,
        schedule=once_schedule(due_at.isoformat()),
        now=due_at - timedelta(minutes=1),
    )
    second = store.add_job(
        name="second",
        yaml_path=yaml_path,
        schedule=once_schedule(due_at.isoformat()),
        now=due_at - timedelta(minutes=1),
    )
    started_path = tmp_path / "agent-started"
    command = [
        sys.executable,
        "-c",
        (f"import pathlib, time; pathlib.Path({str(started_path)!r}).write_text('started'); time.sleep(60)"),
    ]
    service = ScheduleService(
        store,
        runner=ScheduleRunner(
            store,
            command_factory=lambda _job: command,
            poll_seconds=0.01,
        ),
    )
    stop = Event()
    thread = Thread(
        target=service.serve,
        kwargs={"tick_seconds": 0.1, "stop_event": stop},
        daemon=True,
    )

    thread.start()
    try:
        running_pid: int | None = None
        for _ in range(200):
            executions = store.list_executions()
            if started_path.exists() and executions and executions[0]["status"] == "running":
                running_pid = executions[0]["pid"]
                break
            stop.wait(0.01)
        assert running_pid is not None
    finally:
        stop.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert not service._pid_is_alive(running_pid)
    executions = store.list_executions()
    assert len(executions) == 1
    assert executions[0]["job_id"] == first["id"]
    assert executions[0]["status"] == "failed"
    assert executions[0]["error"] == "execution interrupted"
    assert executions[0]["finished_at"] is not None
    assert store.get_job(first["id"])["claim"] is None
    assert store.get_job(second["id"])["claim"] is None
    assert store.get_job(second["id"])["run_count"] == 0
    status = ScheduleService(store).status()
    assert status["state"] == "stopped"
    assert status["claimed_count"] == 0
    assert status["heartbeat"]["stopped_at"] is not None


def test_stop_event_kills_a_due_agent_that_ignores_terminate(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    due_at = datetime(2000, 1, 1, tzinfo=UTC)
    job = store.add_job(
        name="ignores-terminate",
        yaml_path=yaml_path,
        schedule=once_schedule(due_at.isoformat()),
        now=due_at - timedelta(minutes=1),
    )
    started_path = tmp_path / "agent-started"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(started_path)!r}).write_text('started'); "
            "time.sleep(60)"
        ),
    ]
    service = ScheduleService(
        store,
        runner=ScheduleRunner(
            store,
            command_factory=lambda _job: command,
            poll_seconds=0.01,
        ),
    )
    stop = Event()
    thread = Thread(
        target=service.serve,
        kwargs={"tick_seconds": 0.1, "stop_event": stop},
        daemon=True,
    )

    thread.start()
    try:
        running_pid: int | None = None
        for _ in range(200):
            execution = store.list_executions()
            if started_path.exists() and execution and execution[0]["status"] == "running":
                running_pid = execution[0]["pid"]
                break
            stop.wait(0.01)
        assert running_pid is not None
    finally:
        stop.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert not service._pid_is_alive(running_pid)
    execution = store.list_executions()[0]
    assert execution["job_id"] == job["id"]
    assert execution["status"] == "failed"
    assert execution["error"] == "execution interrupted"
    assert store.get_job(job["id"])["claim"] is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
def test_interrupt_kills_agent_descendants(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    job = _job(store, tmp_path)
    descendant_pid_path = tmp_path / "descendant.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"pathlib.Path({str(descendant_pid_path)!r}).write_text(str(child.pid)); "
            "time.sleep(60)"
        ),
    ]
    runner = ScheduleRunner(store, command_factory=lambda _job: command, poll_seconds=0.01)
    descendant_pid: int | None = None
    interrupted = False

    def interrupt_after_descendant_starts() -> None:
        nonlocal interrupted
        if descendant_pid_path.exists() and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    try:
        execution = runner.run_now(job["id"], now=NOW, progress=interrupt_after_descendant_starts)
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 1
        while ScheduleService._pid_is_alive(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert execution["error"] == "execution interrupted"
        assert not ScheduleService._pid_is_alive(descendant_pid)
    finally:
        if descendant_pid is not None and ScheduleService._pid_is_alive(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_python_module_entrypoint_reaches_registered_schedules_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "src", "schedules", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Manage durable project-level Agent schedules" in result.stdout
    assert "serve" in result.stdout
