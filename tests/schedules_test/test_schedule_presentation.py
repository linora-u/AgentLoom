from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agentloom.schedules.presentation import schedule_catalog

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _job(
    job_id: str = "job-report",
    *,
    state: str = "scheduled",
    schedule: dict | None = None,
    next_run_at: str | None = None,
    last_run_at: str | None = None,
    last_status: str | None = None,
    run_count: int = 0,
) -> dict:
    return {
        "id": job_id,
        "name": "hourly report",
        "yaml_path": "applications/demo/workflows/demo.yaml",
        "schedule": schedule
        or {
            "kind": "interval",
            "seconds": 3600,
            "timezone": "UTC",
        },
        "state": state,
        "created_at": (NOW - timedelta(days=2)).isoformat(),
        "updated_at": (NOW - timedelta(minutes=1)).isoformat(),
        "next_run_at": next_run_at,
        "last_run_at": last_run_at,
        "last_status": last_status,
        "run_count": run_count,
        "claim": None,
    }


def _execution(
    execution_id: str,
    *,
    job_id: str = "job-report",
    sequence: int | None = None,
    status: str = "succeeded",
    trigger: str = "manual",
    claimed_at: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    exit_code: int | None = 0,
    error: str | None = None,
) -> dict:
    started = started_at is not None
    execution = {
        "id": execution_id,
        "job_id": job_id,
        "job_name": "hourly report",
        "trigger": trigger,
        "scheduled_for": (claimed_at if trigger == "scheduled" else None),
        "status": status,
        "claimed_at": claimed_at or NOW.isoformat(),
        "started_at": started_at,
        "finished_at": finished_at,
        "command": ["agentloom"] if started else None,
        "pid": 123 if started else None,
        "exit_code": exit_code,
        "stdout_path": f"{execution_id}.stdout.log" if started else None,
        "stderr_path": f"{execution_id}.stderr.log" if started else None,
        "error": error,
    }
    if sequence is not None:
        execution["sequence"] = sequence
    return execution


def _heartbeat(**overrides) -> dict:
    return {
        "pid": os.getpid(),
        "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "last_tick_at": NOW.isoformat(),
        "last_success_at": NOW.isoformat(),
        "last_error": None,
        "tick_seconds": 1.0,
        "stopped_at": None,
        **overrides,
    }


def _write_schedule_document(
    root: Path,
    *,
    jobs: list[object],
    executions: list[object],
    runtime_root: Path | None = None,
) -> None:
    target_root = runtime_root or root / ".agentloom"
    _write(
        target_root / "schedules/jobs.json",
        json.dumps(
            {
                "version": 1,
                "jobs": jobs,
                "executions": executions,
            }
        ),
    )


def test_empty_projection_is_read_only(tmp_path: Path) -> None:
    assert schedule_catalog(tmp_path, now=NOW) == {
        "items": [],
        "service": {
            "state": "stopped",
            "pid": None,
            "started_at": None,
            "last_tick_at": None,
            "last_success_at": None,
            "last_error": None,
            "job_count": 0,
            "due_count": 0,
            "claimed_count": 0,
            "execution_count": 0,
        },
    }
    assert not (tmp_path / ".agentloom").exists()


def test_schedule_package_keeps_execution_exports_lazy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import agentloom.schedules as schedules; "
                "from agentloom.schedules import presentation; "
                "assert presentation.schedule_catalog; "
                "assert 'agentloom.schedules.runner' not in sys.modules; "
                "assert 'agentloom.schedules.service' not in sys.modules; "
                "assert 'agentloom.schedules.store' not in sys.modules; "
                "assert dir(schedules).count('ScheduleStore') == 1; "
                "assert schedules.ScheduleStore; "
                "assert 'agentloom.schedules.store' in sys.modules; "
                "assert dir(schedules).count('ScheduleStore') == 1"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_projection_includes_job_execution_and_service_health(tmp_path: Path) -> None:
    schedules_dir = tmp_path / ".agentloom/schedules"
    schedules_dir.mkdir(parents=True)
    next_run = (NOW + timedelta(hours=1)).isoformat()
    old_execution = _execution(
        "exec-old",
        sequence=1,
        status="failed",
        trigger="scheduled",
        claimed_at=(NOW - timedelta(days=1)).isoformat(),
        started_at=(NOW - timedelta(days=1)).isoformat(),
        finished_at=(NOW - timedelta(days=1) + timedelta(seconds=3)).isoformat(),
        exit_code=1,
        error="old failure",
    )
    latest_execution = _execution(
        "exec-latest",
        sequence=2,
        claimed_at=(NOW - timedelta(minutes=10)).isoformat(),
        started_at=(NOW - timedelta(minutes=10)).isoformat(),
        finished_at=(NOW - timedelta(minutes=9)).isoformat(),
    )
    _write_schedule_document(
        tmp_path,
        jobs=[
            _job(
                next_run_at=next_run,
                last_run_at=latest_execution["finished_at"],
                last_status="succeeded",
                run_count=2,
            )
        ],
        executions=[old_execution, latest_execution],
    )
    _write(
        schedules_dir / "serve-status.json",
        json.dumps(_heartbeat()),
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == [
        {
            "id": "job-report",
            "name": "hourly report",
            "enabled": True,
            "state": "scheduled",
            "yaml_path": "applications/demo/workflows/demo.yaml",
            "trigger": {
                "kind": "interval",
                "seconds": 3600,
                "timezone": "UTC",
            },
            "next_run_at": next_run,
            "last_run_at": latest_execution["finished_at"],
            "last_status": "succeeded",
            "run_count": 2,
            "last_execution": {
                "id": "exec-latest",
                "job_id": "job-report",
                "status": "succeeded",
                "trigger": "manual",
                "claimed_at": (NOW - timedelta(minutes=10)).isoformat(),
                "started_at": (NOW - timedelta(minutes=10)).isoformat(),
                "finished_at": (NOW - timedelta(minutes=9)).isoformat(),
                "exit_code": 0,
                "error": None,
            },
        }
    ]
    assert projection["service"] == {
        "state": "running",
        "pid": os.getpid(),
        "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "last_tick_at": NOW.isoformat(),
        "last_success_at": NOW.isoformat(),
        "last_error": None,
        "job_count": 1,
        "due_count": 0,
        "claimed_count": 0,
        "execution_count": 2,
    }


def test_projection_uses_configured_runtime_root(tmp_path: Path) -> None:
    _write(tmp_path / "config/system.yaml", "runtime:\n  root_dir: state/runtime\n")
    _write_schedule_document(
        tmp_path,
        runtime_root=tmp_path / "state/runtime",
        jobs=[
            _job(
                "job-canonical",
                next_run_at=(NOW + timedelta(hours=1)).isoformat(),
            )
        ],
        executions=[],
    )

    assert [item["id"] for item in schedule_catalog(tmp_path, now=NOW)["items"]] == ["job-canonical"]


def test_projection_does_not_follow_symlinked_system_config(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "system.yaml", "runtime:\n  root_dir: outside-state\n")
    (tmp_path / "config").symlink_to(outside, target_is_directory=True)
    _write(
        tmp_path / "outside-state/schedules/jobs.json",
        json.dumps({"version": 1, "jobs": [{"id": "escaped"}], "executions": []}),
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert not (tmp_path / ".agentloom").exists()


@pytest.mark.parametrize(
    "root_dir",
    [
        "~agentloom-user-that-does-not-exist",
        "x" * 5000,
    ],
)
def test_projection_reports_invalid_runtime_root_as_unreadable(
    tmp_path: Path,
    root_dir: str,
) -> None:
    _write(tmp_path / "config/system.yaml", f"runtime:\n  root_dir: {root_dir!r}\n")

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


def test_projection_reports_invalid_explicit_runtime_root_as_unreadable(
    tmp_path: Path,
) -> None:
    projection = schedule_catalog(
        tmp_path,
        now=NOW,
        runtime_root=Path("~agentloom-user-that-does-not-exist"),
    )

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


def test_projection_anchors_relative_explicit_runtime_root_to_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    outside_cwd = tmp_path / "cwd"
    outside_cwd.mkdir()
    _write_schedule_document(
        tmp_path,
        runtime_root=tmp_path / "relative-runtime",
        jobs=[_job("project-job")],
        executions=[],
    )
    _write_schedule_document(
        outside_cwd,
        runtime_root=outside_cwd / "relative-runtime",
        jobs=[_job("outside-job")],
        executions=[],
    )
    monkeypatch.chdir(outside_cwd)

    projection = schedule_catalog(
        tmp_path,
        now=NOW,
        runtime_root=Path("relative-runtime"),
    )

    assert [item["id"] for item in projection["items"]] == ["project-job"]


def test_explicit_runtime_root_uses_cached_value_when_system_config_breaks(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    _write_schedule_document(
        tmp_path,
        runtime_root=runtime_root,
        jobs=[_job("cached-root")],
        executions=[],
    )
    _write(tmp_path / "config/system.yaml", "{invalid")

    projection = schedule_catalog(
        tmp_path,
        now=NOW,
        runtime_root=runtime_root,
    )

    assert [item["id"] for item in projection["items"]] == ["cached-root"]


@pytest.mark.parametrize("runtime_root", [42, object(), b"invalid"])
def test_projection_reports_non_pathlike_runtime_root_as_unreadable(
    tmp_path: Path,
    runtime_root: object,
) -> None:
    projection = schedule_catalog(
        tmp_path,
        now=NOW,
        runtime_root=runtime_root,  # type: ignore[arg-type]
    )

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


def test_projection_rejects_yaml_alias_expansion_without_hanging(
    tmp_path: Path,
) -> None:
    aliases = ["a0: &a0 [x, x, x, x, x, x, x, x]"]
    aliases.extend(f"a{depth}: &a{depth} [{', '.join([f'*a{depth - 1}'] * 8)}]" for depth in range(1, 9))
    aliases.append("runtime:\n  root_dir: *a8")
    _write(tmp_path / "config/system.yaml", "\n".join(aliases) + "\n")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from agentloom.schedules.presentation import schedule_catalog; "
                "result=schedule_catalog(Path(__import__('sys').argv[1])); "
                "assert result['items'] == []"
            ),
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=1,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_projection_accepts_current_repository_system_config(tmp_path: Path) -> None:
    repository_config = Path(__file__).resolve().parents[2] / "config/system.yaml"
    _write(
        tmp_path / "config/system.yaml",
        repository_config.read_text(encoding="utf-8"),
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"


def test_projection_rejects_symlinked_schedule_document(tmp_path: Path) -> None:
    outside = tmp_path / "outside/jobs.json"
    _write(outside, json.dumps({"version": 1, "jobs": [], "executions": []}))
    schedules_dir = tmp_path / ".agentloom/schedules"
    schedules_dir.mkdir(parents=True)
    (schedules_dir / "jobs.json").symlink_to(outside)

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


def test_projection_rejects_duplicate_job_ids(tmp_path: Path) -> None:
    _write_schedule_document(
        tmp_path,
        jobs=[
            _job("duplicate"),
            _job("duplicate", state="paused"),
        ],
        executions=[_execution("execution", job_id="duplicate")],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


@pytest.mark.parametrize(
    "jobs",
    [
        [17],
        [{}],
        [_job(state="unknown")],
        [{key: value for key, value in _job().items() if key != "created_at"}],
    ],
)
def test_projection_rejects_malformed_job_records(
    tmp_path: Path,
    jobs: list[object],
) -> None:
    _write_schedule_document(tmp_path, jobs=jobs, executions=[])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


def test_projection_reads_legacy_version_one_job_name_beyond_new_write_limit(
    tmp_path: Path,
) -> None:
    job = _job()
    job["name"] = "x" * 513
    _write_schedule_document(tmp_path, jobs=[job], executions=[])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"
    assert projection["items"][0]["name"] == "x" * 513


@pytest.mark.skipif(os.name == "nt", reason="characters follow Windows path rules")
@pytest.mark.parametrize("special_character", ["\\", "\n", "\r"])
def test_projection_accepts_version_one_absolute_path_special_characters(
    tmp_path: Path,
    special_character: str,
) -> None:
    job = _job()
    job["yaml_path"] = str(tmp_path / f"outside{special_character}agent.yaml")
    _write_schedule_document(tmp_path, jobs=[job], executions=[])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"
    assert projection["items"][0]["yaml_path"] == ""


def test_projection_accepts_version_one_long_owner_and_empty_command(
    tmp_path: Path,
) -> None:
    job = _job(next_run_at=(NOW + timedelta(hours=1)).isoformat())
    execution = _execution(
        "exec-running",
        status="running",
        claimed_at=NOW.isoformat(),
        started_at=NOW.isoformat(),
        exit_code=None,
    )
    execution["command"] = []
    job["claim"] = {
        "execution_id": execution["id"],
        "owner": "x" * 4097,
        "claimed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    _write_schedule_document(tmp_path, jobs=[job], executions=[execution])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"
    assert projection["service"]["claimed_count"] == 1
    assert projection["items"][0]["last_execution"]["status"] == "running"


def test_projection_reports_oversized_persisted_timezone_as_error(
    tmp_path: Path,
) -> None:
    job = _job()
    job["schedule"]["timezone"] = "x" * 5000
    _write_schedule_document(tmp_path, jobs=[job], executions=[])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


@pytest.mark.parametrize(
    ("run_count", "last_run_at", "last_status"),
    [
        (0, None, "succeeded"),
        (0, NOW.isoformat(), None),
        (1, None, None),
        (1, NOW.isoformat(), None),
    ],
)
def test_projection_rejects_inconsistent_job_run_summary(
    tmp_path: Path,
    run_count: int,
    last_run_at: str | None,
    last_status: str | None,
) -> None:
    job = _job(
        run_count=run_count,
        last_run_at=last_run_at,
        last_status=last_status,
    )
    _write_schedule_document(tmp_path, jobs=[job], executions=[])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize(
    ("target", "field"),
    [
        ("job", "state"),
        ("job", "last_status"),
        ("execution", "trigger"),
        ("execution", "status"),
    ],
)
def test_projection_rejects_noncanonical_durable_enums(
    tmp_path: Path,
    target: str,
    field: str,
) -> None:
    job = _job()
    execution = _execution(
        "enum-execution",
        status="claimed",
        exit_code=None,
    )
    job["claim"] = {
        "execution_id": execution["id"],
        "owner": "enum-test",
        "claimed_at": execution["claimed_at"],
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    if target == "job":
        if field == "last_status":
            job.update(
                {
                    "run_count": 1,
                    "last_run_at": NOW.isoformat(),
                    "last_status": " succeeded ",
                }
            )
        else:
            job[field] = " scheduled "
    else:
        execution[field] = f" {execution[field]} "
    _write_schedule_document(tmp_path, jobs=[job], executions=[execution])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize(
    ("target", "field"),
    [
        ("job", "id"),
        ("claim", "execution_id"),
        ("execution", "id"),
        ("execution", "job_id"),
    ],
)
def test_projection_rejects_noncanonical_durable_identities(
    tmp_path: Path,
    target: str,
    field: str,
) -> None:
    job = _job()
    execution = _execution(
        "identity-execution",
        status="claimed",
        exit_code=None,
    )
    job["claim"] = {
        "execution_id": execution["id"],
        "owner": "identity-test",
        "claimed_at": execution["claimed_at"],
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    if target == "job":
        job[field] = f" {job[field]} "
    elif target == "claim":
        job["claim"][field] = f" {job['claim'][field]} "
    else:
        execution[field] = f" {execution[field]} "
    _write_schedule_document(tmp_path, jobs=[job], executions=[execution])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize("seconds", [1.0, True])
def test_projection_rejects_noninteger_persisted_interval_seconds(
    tmp_path: Path,
    seconds: object,
) -> None:
    job = _job()
    job["schedule"]["seconds"] = seconds
    _write_schedule_document(tmp_path, jobs=[job], executions=[])

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize(
    "executions",
    [
        [17],
        [{}],
        [_execution("execution", status="unknown")],
        [{key: value for key, value in _execution("execution").items() if key != "command"}],
        [
            _execution("first", sequence=1),
            _execution("second", sequence=1),
        ],
    ],
)
def test_projection_rejects_malformed_execution_records(
    tmp_path: Path,
    executions: list[object],
) -> None:
    _write_schedule_document(
        tmp_path,
        jobs=[_job()],
        executions=executions,
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


def test_projection_prefers_sequence_over_wall_clock(tmp_path: Path) -> None:
    newer_sequence = _execution(
        "newer-sequence",
        sequence=2,
        claimed_at=(NOW - timedelta(days=2)).isoformat(),
        finished_at=(NOW - timedelta(days=2)).isoformat(),
    )
    newer_clock = _execution(
        "newer-clock",
        sequence=1,
        claimed_at=NOW.isoformat(),
        finished_at=NOW.isoformat(),
    )
    _write_schedule_document(
        tmp_path,
        jobs=[_job()],
        executions=[newer_clock, newer_sequence],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"][0]["last_execution"]["id"] == "newer-sequence"


def test_projection_accepts_version_one_unknown_failure_shape(
    tmp_path: Path,
) -> None:
    execution = _execution(
        "unknown-failure",
        status="failed",
        claimed_at=(NOW - timedelta(seconds=1)).isoformat(),
        finished_at=NOW.isoformat(),
        exit_code=None,
        error=None,
    )
    _write_schedule_document(
        tmp_path,
        jobs=[
            _job(
                last_run_at=NOW.isoformat(),
                last_status="failed",
                run_count=1,
            )
        ],
        executions=[execution],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"
    assert projection["items"][0]["last_execution"] == {
        "id": "unknown-failure",
        "job_id": "job-report",
        "status": "failed",
        "trigger": "manual",
        "claimed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "started_at": None,
        "finished_at": NOW.isoformat(),
        "exit_code": None,
        "error": None,
    }


@pytest.mark.parametrize(
    ("legacy_name", "display_name"),
    [
        ("legacy\njob", "legacy job"),
        ("legacy\rjob", "legacy job"),
        ("legacy\x00job", "legacyjob"),
    ],
)
def test_projection_sanitizes_version_one_execution_name_controls(
    tmp_path: Path,
    legacy_name: str,
    display_name: str,
) -> None:
    execution = _execution(
        "legacy-name",
        claimed_at=(NOW - timedelta(seconds=2)).isoformat(),
        started_at=(NOW - timedelta(seconds=1)).isoformat(),
        finished_at=NOW.isoformat(),
    )
    execution["job_name"] = legacy_name
    job = _job(
        last_run_at=NOW.isoformat(),
        last_status="succeeded",
        run_count=1,
    )
    job["name"] = legacy_name
    _write_schedule_document(
        tmp_path,
        jobs=[job],
        executions=[execution],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"
    assert projection["items"][0]["name"] == display_name
    assert projection["items"][0]["last_execution"]["id"] == "legacy-name"


def test_projection_accepts_version_one_nul_error_text(tmp_path: Path) -> None:
    execution = _execution(
        "nul-failure",
        status="failed",
        claimed_at=(NOW - timedelta(seconds=1)).isoformat(),
        finished_at=NOW.isoformat(),
        exit_code=None,
        error="bad\x00message",
    )
    _write_schedule_document(
        tmp_path,
        jobs=[
            _job(
                last_run_at=NOW.isoformat(),
                last_status="failed",
                run_count=1,
            )
        ],
        executions=[execution],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "stopped"
    assert projection["items"][0]["last_execution"]["error"] == "badmessage"


def test_projection_legacy_executions_fall_back_to_timestamps(tmp_path: Path) -> None:
    older = _execution(
        "older",
        claimed_at=(NOW - timedelta(hours=2)).isoformat(),
        finished_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    newer = _execution(
        "newer",
        claimed_at=(NOW - timedelta(hours=1)).isoformat(),
        finished_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    _write_schedule_document(
        tmp_path,
        jobs=[_job()],
        executions=[newer, older],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"][0]["last_execution"]["id"] == "newer"


def test_projection_prefers_sequenced_execution_over_legacy_history(
    tmp_path: Path,
) -> None:
    legacy = _execution(
        "legacy",
        claimed_at=NOW.isoformat(),
        finished_at=NOW.isoformat(),
    )
    sequenced = _execution(
        "sequenced",
        sequence=0,
        claimed_at=(NOW - timedelta(days=1)).isoformat(),
        finished_at=(NOW - timedelta(days=1)).isoformat(),
    )
    _write_schedule_document(
        tmp_path,
        jobs=[_job()],
        executions=[legacy, sequenced],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"][0]["last_execution"]["id"] == "sequenced"


def test_projection_accepts_execution_history_for_removed_job(tmp_path: Path) -> None:
    _write_schedule_document(
        tmp_path,
        jobs=[],
        executions=[
            _execution(
                "orphaned",
                job_id="removed-job",
                sequence=1,
                claimed_at=(NOW - timedelta(seconds=2)).isoformat(),
                started_at=(NOW - timedelta(seconds=1)).isoformat(),
                finished_at=NOW.isoformat(),
            )
        ],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "stopped"
    assert projection["service"]["execution_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    ["ghost-claim", "orphan-active", "completed-next-run", "running-finished"],
)
def test_projection_rejects_impossible_durable_lifecycle(
    tmp_path: Path,
    mutation: str,
) -> None:
    job = _job(next_run_at=(NOW + timedelta(hours=1)).isoformat())
    execution = _execution(
        "exec-active",
        status="claimed",
        exit_code=None,
    )
    job["claim"] = {
        "execution_id": execution["id"],
        "owner": "presentation-test",
        "claimed_at": execution["claimed_at"],
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    if mutation == "ghost-claim":
        job["claim"]["execution_id"] = "exec-missing"
    elif mutation == "orphan-active":
        job["claim"] = None
    elif mutation == "completed-next-run":
        job["state"] = "completed"
    else:
        execution.update(
            {
                "status": "running",
                "started_at": NOW.isoformat(),
                "finished_at": (NOW + timedelta(seconds=1)).isoformat(),
                "command": ["agentloom"],
                "pid": 123,
                "stdout_path": "stdout.log",
                "stderr_path": "stderr.log",
            }
        )

    _write_schedule_document(
        tmp_path,
        jobs=[job],
        executions=[execution],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


def test_projection_accepts_version_one_job_count_within_document_limit(
    tmp_path: Path,
) -> None:
    _write_schedule_document(
        tmp_path,
        jobs=[_job(f"job-{index}") for index in range(4097)],
        executions=[],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert len(projection["items"]) == 4097
    assert projection["service"]["state"] == "stopped"
    assert projection["service"]["job_count"] == 4097


def test_projection_rejects_boolean_document_version(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agentloom/schedules/jobs.json",
        '{"version":true,"jobs":[],"executions":[]}',
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize("sequence", [True, -1, 1.5, (1 << 53)])
def test_projection_rejects_invalid_execution_sequence(
    tmp_path: Path,
    sequence: object,
) -> None:
    execution = _execution("invalid-sequence")
    execution["sequence"] = sequence
    _write_schedule_document(
        tmp_path,
        jobs=[_job()],
        executions=[execution],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize(
    "document",
    [
        '{"version":1,"version":1,"jobs":[],"executions":[]}',
        ('{"version":1,"jobs":[{"id":"a","id":"b"}],"executions":[]}'),
        (
            '{"version":1,"jobs":[{"id":"a","name":"a","yaml_path":"a.yaml",'
            '"schedule":{"kind":"interval","kind":"cron","seconds":1,'
            '"timezone":"UTC"},"state":"scheduled","created_at":'
            '"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z",'
            '"next_run_at":null,"last_run_at":null,"last_status":null,'
            '"run_count":0,"claim":null}],"executions":[]}'
        ),
    ],
)
def test_projection_rejects_duplicate_json_keys(
    tmp_path: Path,
    document: str,
) -> None:
    _write(tmp_path / ".agentloom/schedules/jobs.json", document)

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"


def test_projection_reports_unparseable_large_integer_as_unreadable(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / ".agentloom/schedules/jobs.json",
        '{"version":1,"jobs":[],"executions":[],"invalid":' + ("9" * 5000) + "}",
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


def test_projection_rejects_integer_above_javascript_safe_range(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / ".agentloom/schedules/jobs.json",
        (f'{{"version":1,"jobs":[],"executions":[],"unsafe":{1 << 53}}}'),
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["service"]["state"] == "error"


@pytest.mark.parametrize("invalid_constant", ["NaN", "Infinity", "-Infinity"])
def test_projection_rejects_nonstandard_json_constants(
    tmp_path: Path,
    invalid_constant: str,
) -> None:
    _write(
        tmp_path / ".agentloom/schedules/jobs.json",
        (f'{{"version":1,"jobs":[],"executions":[],"invalid":{invalid_constant}}}'),
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


def test_projection_rejects_float_exponent_overflow(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agentloom/schedules/jobs.json",
        '{"version":1,"jobs":[],"executions":[],"invalid":1e1000000}',
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
    assert projection["service"]["last_error"] == "Schedule storage is unreadable."


@pytest.mark.parametrize(
    ("heartbeat_field", "value"),
    [
        ("pid", 10**100),
        ("tick_seconds", 10**1000),
        ("tick_seconds", 10**100),
    ],
)
def test_projection_rejects_oversized_heartbeat_numbers(
    tmp_path: Path,
    heartbeat_field: str,
    value: int,
) -> None:
    _write(
        tmp_path / ".agentloom/schedules/serve-status.json",
        json.dumps(
            _heartbeat(
                last_tick_at=(NOW - timedelta(minutes=1)).isoformat(),
                **{heartbeat_field: value},
            )
        ),
    )

    service = schedule_catalog(tmp_path, now=NOW)["service"]

    assert service["state"] == "error"
    assert service["last_error"] == "Schedule storage is unreadable."


def test_projection_reports_unreadable_heartbeat_as_error(tmp_path: Path) -> None:
    _write(tmp_path / ".agentloom/schedules/serve-status.json", "{invalid")

    service = schedule_catalog(tmp_path, now=NOW)["service"]

    assert service["state"] == "error"
    assert service["last_error"] == "Schedule storage is unreadable."


@pytest.mark.parametrize(
    "heartbeat",
    [
        {"stopped_at": True},
        {key: value for key, value in _heartbeat().items() if key != "last_error"},
        _heartbeat(stopped_at="garbage", pid=None),
        _heartbeat(stopped_at="false", pid=None),
    ],
)
def test_projection_rejects_malformed_heartbeat(
    tmp_path: Path,
    heartbeat: dict,
) -> None:
    _write(
        tmp_path / ".agentloom/schedules/serve-status.json",
        json.dumps(heartbeat),
    )

    service = schedule_catalog(tmp_path, now=NOW)["service"]

    assert service["state"] == "error"
    assert service["last_error"] == "Schedule storage is unreadable."


def test_projection_accepts_complete_stopped_heartbeat(tmp_path: Path) -> None:
    stopped_at = (NOW + timedelta(seconds=1)).isoformat()
    _write(
        tmp_path / ".agentloom/schedules/serve-status.json",
        json.dumps(_heartbeat(pid=None, stopped_at=stopped_at)),
    )

    service = schedule_catalog(tmp_path, now=NOW)["service"]

    assert service["state"] == "stopped"
    assert service["pid"] is None


def test_projection_rejects_duplicate_heartbeat_key(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agentloom/schedules/serve-status.json",
        (
            '{"pid":null,"started_at":null,"last_tick_at":"2026-01-01T00:00:00Z",'
            '"last_success_at":null,"last_error":null,"tick_seconds":1,'
            '"stopped_at":"2026-01-01T00:00:00Z","stopped_at":null}'
        ),
    )

    service = schedule_catalog(tmp_path, now=NOW)["service"]

    assert service["state"] == "error"


def test_projection_does_not_trust_future_heartbeat(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agentloom/schedules/serve-status.json",
        json.dumps(
            _heartbeat(
                last_tick_at=(NOW + timedelta(days=1)).isoformat(),
            )
        ),
    )

    service = schedule_catalog(tmp_path, now=NOW)["service"]

    assert service["state"] == "stale"


def test_projection_rejects_iso_timestamp_that_overflows_utc_conversion(
    tmp_path: Path,
) -> None:
    _write_schedule_document(
        tmp_path,
        jobs=[
            _job(
                "boundary-time",
                next_run_at="0001-01-01T00:00:00+14:00",
            )
        ],
        executions=[],
    )

    projection = schedule_catalog(tmp_path, now=NOW)

    assert projection["items"] == []
    assert projection["service"]["state"] == "error"
