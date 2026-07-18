from __future__ import annotations

import json
import multiprocessing
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.schedules.schedule import interval_schedule, once_schedule
from src.schedules.store import JobBusyError, ScheduleStore

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def _claim_once(project_root: str, queue) -> None:
    store = ScheduleStore(Path(project_root))
    queue.put(len(store.claim_due(now=NOW, owner=f"worker-{multiprocessing.current_process().pid}")))


def _add_due_job(store: ScheduleStore, yaml_path: Path) -> dict:
    return store.add_job(
        name="daily-report",
        yaml_path=yaml_path,
        schedule=once_schedule(NOW.isoformat(), timezone="UTC"),
        now=NOW - timedelta(minutes=1),
    )


def _finish_manual_execution(
    store: ScheduleStore,
    job_id: str,
    *,
    sequence: int,
    success: bool = True,
    with_logs: bool = False,
) -> str:
    claimed_at = NOW + timedelta(seconds=sequence * 2)
    claim = store.claim_now(job_id, owner="retention-test", now=claimed_at)
    execution_id = str(claim["execution"]["id"])
    stdout_path = f".agentloom/schedules/executions/{execution_id}.stdout.log"
    stderr_path = f".agentloom/schedules/executions/{execution_id}.stderr.log"
    if with_logs:
        with store.open_execution_logs(execution_id) as (stdout, stderr):
            stdout.write(f"stdout-{sequence}".encode())
            stderr.write(f"stderr-{sequence}".encode())
    store.finish_execution(
        execution_id,
        exit_code=0 if success else 1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        error=None if success else f"failed-{sequence}",
        now=claimed_at + timedelta(seconds=1),
    )
    return execution_id


@pytest.mark.parametrize("symlinked_component", [".agentloom", "schedules"])
def test_add_job_rejects_symlinked_schedule_directory_components(
    tmp_path: Path,
    symlinked_component: str,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    if symlinked_component == ".agentloom":
        (tmp_path / ".agentloom").symlink_to(outside, target_is_directory=True)
        escaped_jobs = outside / "schedules/jobs.json"
    else:
        agentloom_dir = tmp_path / ".agentloom"
        agentloom_dir.mkdir()
        (agentloom_dir / "schedules").symlink_to(outside, target_is_directory=True)
        escaped_jobs = outside / "jobs.json"

    with pytest.raises((OSError, RuntimeError)):
        _add_due_job(ScheduleStore(tmp_path), yaml_path)

    assert not escaped_jobs.exists()


def test_store_rejects_symlinked_jobs_and_lock_targets(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    store.schedules_dir.mkdir(parents=True, exist_ok=True)
    outside_jobs = tmp_path / "outside-jobs.json"
    outside_jobs.write_text('{"version": 1, "jobs": [], "executions": []}\n', encoding="utf-8")
    store.jobs_path.symlink_to(outside_jobs)

    with pytest.raises((OSError, RuntimeError)):
        _add_due_job(store, yaml_path)

    assert json.loads(outside_jobs.read_text(encoding="utf-8"))["jobs"] == []

    store.jobs_path.unlink()
    store.lock_path.unlink()
    outside_lock = tmp_path / "outside.lock"
    outside_lock.write_text("sentinel", encoding="utf-8")
    store.lock_path.symlink_to(outside_lock)
    with pytest.raises((OSError, RuntimeError)):
        store.list_jobs()
    assert outside_lock.read_text(encoding="utf-8") == "sentinel"


def test_store_remains_anchored_when_agentloom_path_is_swapped_after_init(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    (tmp_path / ".agentloom/schedules").mkdir(parents=True)
    store = ScheduleStore(tmp_path)
    anchored_agentloom = tmp_path / ".agentloom-anchored"
    (tmp_path / ".agentloom").rename(anchored_agentloom)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agentloom").symlink_to(outside, target_is_directory=True)

    _add_due_job(store, yaml_path)

    assert (anchored_agentloom / "schedules/jobs.json").is_file()
    assert not (outside / "schedules/jobs.json").exists()


def test_store_is_project_scoped_and_writes_a_versioned_json_document(tmp_path: Path) -> None:
    yaml_path = tmp_path / "applications/demo/workflows/report.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text("name: report\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)

    job = _add_due_job(store, yaml_path)
    payload = json.loads(store.jobs_path.read_text(encoding="utf-8"))

    assert store.jobs_path == tmp_path / ".agentloom/schedules/jobs.json"
    assert payload["version"] == 1
    assert payload["jobs"][0]["id"] == job["id"]
    assert payload["jobs"][0]["yaml_path"] == "applications/demo/workflows/report.yaml"
    assert payload["executions"] == []


def test_add_validation_failure_is_never_visible_to_a_concurrent_ticker(tmp_path: Path) -> None:
    yaml_path = tmp_path / "applications/demo/workflows/report.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text("name: report\n", encoding="utf-8")
    validator_entered = threading.Event()
    release_validator = threading.Event()
    claim_started = threading.Event()
    claim_finished = threading.Event()
    add_errors: list[BaseException] = []
    claims: list[dict] = []

    def reject_job(job: dict) -> None:
        assert job["yaml_path"] == "applications/demo/workflows/report.yaml"
        validator_entered.set()
        assert release_validator.wait(timeout=5)
        raise ValueError("target changed")

    def add_job() -> None:
        try:
            ScheduleStore(tmp_path).add_job(
                name="unsafe",
                yaml_path=yaml_path,
                schedule=once_schedule(NOW.isoformat(), timezone="UTC"),
                now=NOW - timedelta(minutes=1),
                validate_before_commit=reject_job,
            )
        except BaseException as error:
            add_errors.append(error)

    def claim_due() -> None:
        claim_started.set()
        claims.extend(ScheduleStore(tmp_path).claim_due(now=NOW, owner="ticker"))
        claim_finished.set()

    add_thread = threading.Thread(target=add_job)
    add_thread.start()
    assert validator_entered.wait(timeout=5)
    claim_thread = threading.Thread(target=claim_due)
    claim_thread.start()
    assert claim_started.wait(timeout=5)
    assert not claim_finished.wait(timeout=0.1)
    assert not ScheduleStore(tmp_path).jobs_path.exists()

    release_validator.set()
    add_thread.join(timeout=5)
    claim_thread.join(timeout=5)

    assert not add_thread.is_alive()
    assert not claim_thread.is_alive()
    assert len(add_errors) == 1
    assert isinstance(add_errors[0], ValueError)
    assert claims == []
    assert ScheduleStore(tmp_path).list_jobs() == []


def test_cross_process_claim_allows_only_one_execution_for_one_fire(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    _add_due_job(store, yaml_path)

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_claim_once, args=(str(tmp_path), queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    assert sorted(queue.get(timeout=2) for _ in workers) == [0, 1]
    snapshot = store.snapshot()
    assert len(snapshot["executions"]) == 1
    assert snapshot["executions"][0]["status"] == "claimed"


def test_pause_resume_and_manual_claim_preserve_the_scheduled_fire(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    job = store.add_job(
        name="repeating",
        yaml_path=yaml_path,
        schedule=interval_schedule("1h"),
        now=NOW,
    )
    scheduled_at = job["next_run_at"]

    paused = store.pause(job["id"], now=NOW + timedelta(minutes=1))
    assert paused["state"] == "paused"
    assert paused["next_run_at"] == scheduled_at
    assert store.claim_due(now=NOW + timedelta(hours=2), owner="ticker") == []

    resumed = store.resume(job["id"], now=NOW + timedelta(hours=2))
    assert resumed["state"] == "scheduled"
    assert resumed["next_run_at"] == (NOW + timedelta(hours=3)).isoformat()

    claim = store.claim_now(job["id"], owner="operator", now=NOW + timedelta(hours=2))
    with pytest.raises(JobBusyError):
        store.claim_now(job["id"], owner="another", now=NOW + timedelta(hours=2))
    execution_id = claim["execution"]["id"]
    finished = store.finish_execution(
        execution_id,
        exit_code=0,
        stdout_path=f".agentloom/schedules/executions/{execution_id}.stdout.log",
        stderr_path=f".agentloom/schedules/executions/{execution_id}.stderr.log",
        now=NOW + timedelta(hours=2, seconds=2),
    )

    assert finished["status"] == "succeeded"
    assert store.get_job(job["id"])["next_run_at"] == resumed["next_run_at"]


def test_scheduled_completion_advances_recurring_job_and_consumes_once(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    recurring = store.add_job(
        name="repeat",
        yaml_path=yaml_path,
        schedule=interval_schedule("10m"),
        now=NOW - timedelta(minutes=10),
    )
    once = _add_due_job(store, yaml_path)

    claims = store.claim_due(now=NOW, owner="ticker", limit=2)
    assert {claim["job"]["id"] for claim in claims} == {recurring["id"], once["id"]}
    for claim in claims:
        execution_id = claim["execution"]["id"]
        store.finish_execution(
            execution_id,
            exit_code=0,
            stdout_path=f".agentloom/schedules/executions/{execution_id}.stdout.log",
            stderr_path=f".agentloom/schedules/executions/{execution_id}.stderr.log",
            now=NOW + timedelta(seconds=2),
        )

    recurring_after = store.get_job(recurring["id"])
    once_after = store.get_job(once["id"])
    assert recurring_after["next_run_at"] == (NOW + timedelta(minutes=10)).isoformat()
    assert once_after["state"] == "completed"
    assert once_after["next_run_at"] is None


def test_remove_deletes_job_but_retains_its_execution_ledger(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    job = _add_due_job(store, yaml_path)
    claim = store.claim_due(now=NOW, owner="ticker")[0]
    execution_id = claim["execution"]["id"]
    store.finish_execution(
        execution_id,
        exit_code=1,
        stdout_path=f".agentloom/schedules/executions/{execution_id}.stdout.log",
        stderr_path=f".agentloom/schedules/executions/{execution_id}.stderr.log",
        error="agent failed",
        now=NOW + timedelta(seconds=1),
    )

    store.remove(job["id"])

    assert store.list_jobs() == []
    assert store.snapshot()["executions"][0]["error"] == "agent failed"


def test_execution_retention_bounds_store_and_preserves_job_totals(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(
        tmp_path,
        execution_retention_global=5,
        execution_retention_per_job=3,
    )
    jobs = [
        store.add_job(
            name=f"job-{index}",
            yaml_path=yaml_path,
            schedule=interval_schedule("1h"),
            now=NOW,
        )
        for index in range(3)
    ]
    execution_ids: list[str] = []
    for sequence in range(12):
        execution_ids.append(
            _finish_manual_execution(
                store,
                jobs[sequence % len(jobs)]["id"],
                sequence=sequence,
                success=sequence % 4 != 3,
                with_logs=True,
            )
        )

    snapshot = store.snapshot()
    retained = snapshot["executions"]
    assert len(retained) == 5
    assert all(sum(item["job_id"] == job["id"] for item in retained) <= 3 for job in jobs)
    assert [item["sequence"] for item in store.list_executions()] == sorted(
        (item["sequence"] for item in retained),
        reverse=True,
    )
    assert {item["id"] for item in retained} == set(execution_ids[-5:])
    for job in jobs:
        stored = store.get_job(job["id"])
        assert stored["run_count"] == 4
        assert stored["last_run_at"] is not None
        assert stored["last_status"] in {"succeeded", "failed"}
    assert store.jobs_path.stat().st_size < 64 * 1024

    retained_ids = {item["id"] for item in retained}
    for execution_id in execution_ids:
        for suffix in ("stdout.log", "stderr.log"):
            path = store.executions_dir / f"{execution_id}.{suffix}"
            assert path.exists() is (execution_id in retained_ids)


def test_active_execution_survives_retention_and_can_finish(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(
        tmp_path,
        execution_retention_global=2,
        execution_retention_per_job=2,
        claim_lease_seconds=60,
    )
    job = store.add_job(
        name="active",
        yaml_path=yaml_path,
        schedule=interval_schedule("1h"),
        now=NOW,
    )
    for sequence in range(4):
        _finish_manual_execution(store, job["id"], sequence=sequence)

    claim = store.claim_now(
        job["id"],
        owner="active-owner",
        now=NOW + timedelta(minutes=1),
    )
    execution_id = str(claim["execution"]["id"])

    # Active rows are outside the terminal-history cap and are never evicted.
    assert len(store.snapshot()["executions"]) == 3
    assert store.get_execution(execution_id)["status"] == "claimed"
    assert store.heartbeat_claim(execution_id, now=NOW + timedelta(minutes=1, seconds=1))
    finished = store.finish_execution(
        execution_id,
        exit_code=0,
        stdout_path=f".agentloom/schedules/executions/{execution_id}.stdout.log",
        stderr_path=f".agentloom/schedules/executions/{execution_id}.stderr.log",
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert finished["status"] == "succeeded"
    assert len(store.snapshot()["executions"]) == 2
    assert store.list_executions()[0]["id"] == execution_id
    assert store.get_job(job["id"])["run_count"] == 5


def test_remove_keeps_bounded_latest_ledger_and_cumulative_job_state(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(
        tmp_path,
        execution_retention_global=4,
        execution_retention_per_job=2,
    )
    job = store.add_job(
        name="removed",
        yaml_path=yaml_path,
        schedule=interval_schedule("1h"),
        now=NOW,
    )
    execution_ids = [
        _finish_manual_execution(
            store,
            job["id"],
            sequence=sequence,
            success=sequence != 4,
        )
        for sequence in range(5)
    ]

    removed = store.remove(job["id"])

    assert removed["run_count"] == 5
    assert removed["last_status"] == "failed"
    assert removed["last_run_at"] == (NOW + timedelta(seconds=9)).isoformat()
    assert store.list_jobs() == []
    ledger = store.list_executions(job_id=job["id"])
    assert [item["id"] for item in ledger] == list(reversed(execution_ids[-2:]))


def test_retention_never_follows_symlinked_execution_log(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(
        tmp_path,
        execution_retention_global=1,
        execution_retention_per_job=1,
    )
    job = store.add_job(
        name="logs",
        yaml_path=yaml_path,
        schedule=interval_schedule("1h"),
        now=NOW,
    )
    first_id = _finish_manual_execution(
        store,
        job["id"],
        sequence=0,
        with_logs=True,
    )
    first_stdout = store.executions_dir / f"{first_id}.stdout.log"
    first_stdout.unlink()
    outside = tmp_path / "outside.log"
    outside.write_text("sentinel", encoding="utf-8")
    first_stdout.symlink_to(outside)

    _finish_manual_execution(
        store,
        job["id"],
        sequence=1,
        with_logs=True,
    )

    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert first_stdout.is_symlink()
    assert not (store.executions_dir / f"{first_id}.stderr.log").exists()
    assert all(item["id"] != first_id for item in store.list_executions())


def test_execution_fields_are_bounded_and_log_paths_are_canonical(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("name: test\n", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    job = store.add_job(
        name="界" * 2_000,
        yaml_path=yaml_path,
        schedule=interval_schedule("1h"),
        now=NOW,
    )
    claim = store.claim_now(job["id"], owner="bounds", now=NOW)
    execution_id = str(claim["execution"]["id"])
    stdout_path = f".agentloom/schedules/executions/{execution_id}.stdout.log"
    stderr_path = f".agentloom/schedules/executions/{execution_id}.stderr.log"

    with pytest.raises(ValueError, match="canonical execution log paths"):
        store.mark_running(
            execution_id,
            command=["agentloom"],
            pid=123,
            stdout_path="../../outside.log",
            stderr_path=stderr_path,
            now=NOW,
        )

    running = store.mark_running(
        execution_id,
        command=["界" * 2_000 for _ in range(100)],
        pid=123,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        now=NOW,
    )
    finished = store.finish_execution(
        execution_id,
        exit_code=1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        error="错" * 10_000,
        now=NOW + timedelta(seconds=1),
    )

    assert sum(len(item.encode()) for item in running["command"]) <= store.EXECUTION_COMMAND_MAX_BYTES
    assert len(running["command"]) <= store.EXECUTION_COMMAND_MAX_ITEMS
    assert len(finished["error"].encode()) <= store.EXECUTION_ERROR_MAX_BYTES
    assert len(finished["job_name"].encode()) <= store.EXECUTION_JOB_NAME_MAX_BYTES
    assert finished["stdout_path"] == stdout_path
    assert finished["stderr_path"] == stderr_path


def test_default_retention_keeps_maximum_sized_execution_ledger_below_tui_limit(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path)
    payload = store._empty()
    for sequence in range(600):
        execution_id = f"exec_{sequence:032x}"
        payload["executions"].append(
            {
                "id": execution_id,
                "sequence": sequence,
                "job_id": f"job_{sequence % 10}",
                "job_name": "n" * store.EXECUTION_JOB_NAME_MAX_BYTES,
                "trigger": "manual",
                "scheduled_for": None,
                "status": "failed",
                "claimed_at": (NOW + timedelta(seconds=sequence)).isoformat(),
                "started_at": (NOW + timedelta(seconds=sequence)).isoformat(),
                "finished_at": (NOW + timedelta(seconds=sequence + 1)).isoformat(),
                "command": ["c" * store.EXECUTION_COMMAND_MAX_BYTES],
                "pid": 1,
                "exit_code": 1,
                "stdout_path": f".agentloom/schedules/executions/{execution_id}.stdout.log",
                "stderr_path": f".agentloom/schedules/executions/{execution_id}.stderr.log",
                "error": "e" * store.EXECUTION_ERROR_MAX_BYTES,
            }
        )

    with store._locked(exclusive=True):
        store._write_unlocked(payload)

    persisted = store.snapshot()
    assert len(persisted["executions"]) == 512
    assert store.jobs_path.stat().st_size < 8 * 1024 * 1024
