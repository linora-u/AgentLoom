from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.schedules.store import ScheduleStore
from src.tui_bridge.bridge import BridgeError, TuiBridge


def _write_agent(project_root: Path, relative_path: str, *, name: str = "scheduled_agent") -> Path:
    model_config = project_root / "config/llm.yaml"
    if not model_config.exists():
        model_config.parent.mkdir(parents=True, exist_ok=True)
        model_config.write_text(
            """
model:
  default_model_type: powerful
  powerful:
    model: openai/test-model
""".strip(),
            encoding="utf-8",
        )
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
name: {name}
description: A scheduled supervisor.
workflow: |
  Complete the scheduled task.
""".strip(),
        encoding="utf-8",
    )
    return path


def _add(
    bridge: TuiBridge,
    yaml_path: str,
    schedule: dict[str, object],
    *,
    name: str = "",
) -> dict[str, object]:
    return bridge.dispatch(
        "schedule.add",
        {"yaml_path": yaml_path, "name": name, "schedule": schedule},
    )


def test_schedule_add_parses_once_interval_and_cron_without_using_the_cli(tmp_path: Path) -> None:
    once_path = "applications/periodic/workflows/once.yaml"
    interval_path = "applications/periodic/workflows/hourly.yaml"
    cron_path = "applications/periodic/workflows/morning.yaml"
    _write_agent(tmp_path, once_path, name="once")
    _write_agent(tmp_path, interval_path, name="hourly")
    _write_agent(tmp_path, cron_path, name="morning")
    bridge = TuiBridge(tmp_path)

    once = _add(
        bridge,
        once_path,
        {"kind": "once", "at": "2099-01-02T03:04:05Z", "timezone": "UTC"},
    )
    interval = _add(
        bridge,
        interval_path,
        {"kind": "interval", "every": "90m", "timezone": "UTC"},
        name="Hourly report",
    )
    cron = _add(
        bridge,
        cron_path,
        {"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "Asia/Shanghai"},
    )

    assert interval == {
        "action": "add",
        "job_id": interval["job_id"],
        "name": "Hourly report",
        "state": "scheduled",
    }
    assert isinstance(interval["job_id"], str) and interval["job_id"].startswith("job_")
    assert cron == {
        "action": "add",
        "job_id": cron["job_id"],
        "name": "morning",
        "state": "scheduled",
    }
    jobs = {job["id"]: job for job in ScheduleStore(tmp_path).list_jobs()}
    assert jobs[once["job_id"]]["schedule"] == {
        "kind": "once",
        "at": "2099-01-02T03:04:05+00:00",
        "timezone": "UTC",
    }
    assert jobs[interval["job_id"]]["schedule"] == {
        "kind": "interval",
        "seconds": 5400,
        "timezone": "UTC",
    }
    assert jobs[cron["job_id"]]["schedule"] == {
        "kind": "cron",
        "expression": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
    }


def test_schedule_mutations_are_durable_and_bootstrap_projects_the_result(tmp_path: Path) -> None:
    yaml_path = "applications/durable/workflows/durable.yaml"
    _write_agent(tmp_path, yaml_path, name="durable")
    bridge = TuiBridge(tmp_path)
    added = _add(
        bridge,
        yaml_path,
        {"kind": "interval", "every": "1h", "timezone": "UTC"},
        name="Durable job",
    )
    job_id = str(added["job_id"])

    assert bridge.dispatch("schedule.pause", {"job_id": job_id}) == {
        "action": "pause",
        "job_id": job_id,
        "name": "Durable job",
        "state": "paused",
    }
    paused_catalog = TuiBridge(tmp_path).bootstrap()["schedules"]["items"]
    assert [(item["id"], item["state"], item["enabled"]) for item in paused_catalog] == [
        (
            job_id,
            "paused",
            False,
        )
    ]

    assert bridge.dispatch("schedule.resume", {"job_id": job_id}) == {
        "action": "resume",
        "job_id": job_id,
        "name": "Durable job",
        "state": "scheduled",
    }
    resumed_catalog = TuiBridge(tmp_path).bootstrap()["schedules"]["items"]
    assert [(item["id"], item["state"], item["enabled"]) for item in resumed_catalog] == [
        (
            job_id,
            "scheduled",
            True,
        )
    ]

    assert bridge.dispatch("schedule.remove", {"job_id": job_id}) == {
        "action": "remove",
        "job_id": job_id,
        "name": "Durable job",
        "state": "scheduled",
    }
    assert TuiBridge(tmp_path).bootstrap()["schedules"]["items"] == []
    assert ScheduleStore(tmp_path).list_jobs() == []


@pytest.mark.parametrize("method", ["schedule.pause", "schedule.resume", "schedule.remove"])
def test_schedule_mutations_preserve_unknown_job_errors(method: str, tmp_path: Path) -> None:
    with pytest.raises(BridgeError) as error:
        TuiBridge(tmp_path).dispatch(method, {"job_id": "job_missing"})

    assert error.value.code == "not_found"
    assert str(error.value) == "Unknown schedule job: job_missing"


@pytest.mark.parametrize("method", ["schedule.resume", "schedule.remove"])
def test_schedule_mutations_preserve_live_claim_busy_errors(method: str, tmp_path: Path) -> None:
    yaml_path = "applications/busy/workflows/busy.yaml"
    _write_agent(tmp_path, yaml_path, name="busy")
    added = _add(
        TuiBridge(tmp_path),
        yaml_path,
        {"kind": "interval", "every": "1h", "timezone": "UTC"},
    )
    ScheduleStore(tmp_path).claim_now(str(added["job_id"]), owner="test-owner")

    with pytest.raises(BridgeError) as error:
        TuiBridge(tmp_path).dispatch(method, {"job_id": added["job_id"]})

    assert error.value.code == "busy"
    assert "running" in str(error.value)


@pytest.mark.parametrize("method", ["schedule.run", "schedule.runNow"])
def test_schedule_rpc_does_not_expose_blocking_run_now(method: str, tmp_path: Path) -> None:
    with pytest.raises(BridgeError) as error:
        TuiBridge(tmp_path).dispatch(method, {"job_id": "job_1"})

    assert error.value.code == "method_not_found"


@pytest.mark.parametrize(
    "target_factory",
    [
        pytest.param(lambda root, outside: str(outside), id="absolute-outside"),
        pytest.param(lambda root, outside: "../outside.yaml", id="parent-traversal"),
        pytest.param(
            lambda root, outside: "applications/app/workflows/worker_agents/worker.yaml",
            id="worker-agent",
        ),
        pytest.param(
            lambda root, outside: "applications/app/workflows/missing.yaml",
            id="missing",
        ),
    ],
)
def test_schedule_add_rejects_non_supervisor_targets(tmp_path: Path, target_factory) -> None:
    outside = _write_agent(tmp_path.parent, "outside.yaml")
    _write_agent(tmp_path, "applications/app/workflows/worker_agents/worker.yaml")
    target = target_factory(tmp_path, outside)

    with pytest.raises(BridgeError) as error:
        _add(
            TuiBridge(tmp_path),
            target,
            {"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert error.value.code == "invalid_params"
    assert "yaml_path" in str(error.value)
    assert not (tmp_path / ".agentloom/schedules/jobs.json").exists()


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_schedule_add_rejects_symlink_targets(tmp_path: Path, link_kind: str) -> None:
    outside = _write_agent(tmp_path.parent, f"outside-{link_kind}.yaml")
    if link_kind == "file":
        link = tmp_path / "applications/app/workflows/linked.yaml"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        target = "applications/app/workflows/linked.yaml"
    else:
        workflows = tmp_path / "applications/app/workflows"
        workflows.parent.mkdir(parents=True)
        workflows.symlink_to(outside.parent, target_is_directory=True)
        target = f"applications/app/workflows/{outside.name}"

    with pytest.raises(BridgeError) as error:
        _add(
            TuiBridge(tmp_path),
            target,
            {"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert error.value.code == "invalid_params"
    assert "yaml_path" in str(error.value)
    assert not (tmp_path / ".agentloom/schedules/jobs.json").exists()


def test_schedule_add_rejects_invalid_supervisor_yaml(tmp_path: Path) -> None:
    yaml_path = "applications/invalid/workflows/invalid.yaml"
    path = _write_agent(tmp_path, yaml_path)
    path.write_text("this: is not an Agent supervisor\n", encoding="utf-8")

    with pytest.raises(BridgeError) as error:
        _add(
            TuiBridge(tmp_path),
            yaml_path,
            {"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert error.value.code == "invalid_params"
    assert "valid supervisor" in str(error.value)
    assert not (tmp_path / ".agentloom/schedules/jobs.json").exists()


def test_schedule_add_never_publishes_a_target_swapped_during_store_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_path = "applications/racy/workflows/racy.yaml"
    target = _write_agent(tmp_path, yaml_path)
    outside = _write_agent(tmp_path.parent, "racy-outside.yaml")
    original_stored_path = ScheduleStore._stored_yaml_path
    original_add_job = ScheduleStore.add_job
    due_claims: list[dict[str, object]] = []

    def swap_before_store_resolve(store: ScheduleStore, path: str | Path) -> str:
        target.unlink()
        target.symlink_to(outside)
        return original_stored_path(store, path)

    def add_then_probe_ticker(store: ScheduleStore, **kwargs):
        try:
            return original_add_job(store, **kwargs)
        finally:
            due_claims.extend(
                ScheduleStore(tmp_path).claim_due(
                    now=datetime.now(UTC) + timedelta(days=1),
                    owner="concurrent-ticker-probe",
                )
            )

    monkeypatch.setattr(ScheduleStore, "_stored_yaml_path", swap_before_store_resolve)
    monkeypatch.setattr(ScheduleStore, "add_job", add_then_probe_ticker)

    with pytest.raises(BridgeError) as error:
        _add(
            TuiBridge(tmp_path),
            yaml_path,
            {"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert error.value.code == "invalid_params"
    assert due_claims == []
    assert ScheduleStore(tmp_path).list_jobs() == []


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("schedule.add", {}),
        (
            "schedule.add",
            {
                "yaml_path": "applications/app/workflows/agent.yaml",
                "name": "job",
                "schedule": {"kind": "interval", "every": "1h", "timezone": "UTC"},
                "extra": True,
            },
        ),
        (
            "schedule.add",
            {
                "yaml_path": 3,
                "name": "job",
                "schedule": {"kind": "interval", "every": "1h", "timezone": "UTC"},
            },
        ),
        (
            "schedule.add",
            {
                "yaml_path": "applications/app/workflows/agent.yaml",
                "name": None,
                "schedule": {"kind": "interval", "every": "1h", "timezone": "UTC"},
            },
        ),
        (
            "schedule.add",
            {
                "yaml_path": "applications/app/workflows/agent.yaml",
                "name": "job",
                "schedule": {"kind": "interval", "seconds": 3600, "timezone": "UTC"},
            },
        ),
        (
            "schedule.add",
            {
                "yaml_path": "applications/app/workflows/agent.yaml",
                "name": "job",
                "schedule": {"kind": "cron", "expression": "bad cron", "timezone": "UTC"},
            },
        ),
        (
            "schedule.add",
            {
                "yaml_path": "applications/app/workflows/agent.yaml",
                "name": "job",
                "schedule": {"kind": "later", "timezone": "UTC"},
            },
        ),
        (
            "schedule.add",
            {
                "yaml_path": "applications/app/workflows/agent.yaml",
                "name": "job",
                "schedule": {"kind": [], "timezone": "UTC"},
            },
        ),
        ("schedule.pause", {"job_id": ""}),
        ("schedule.pause", {"job_id": " job_1 "}),
        ("schedule.resume", {"job_id": 4}),
        ("schedule.remove", {"job_id": "job_1", "extra": False}),
    ],
)
def test_schedule_rpc_strictly_validates_wire_params(
    method: str,
    params: dict[str, object],
    tmp_path: Path,
) -> None:
    with pytest.raises(BridgeError) as error:
        TuiBridge(tmp_path).dispatch(method, params)

    assert error.value.code == "invalid_params"
