from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
CONTEXT_REF = "ctx_0123456789abcdef"
CONTEXT_PAYLOAD = "original payload survives runtime migration"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_workflow(repo_root: Path, application_id: str) -> Path:
    workflow = repo_root / "applications" / application_id / "workflows" / "agent.yaml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        "name: supervisor\ndescription: migration test\nworkflow: resume safely\n",
        encoding="utf-8",
    )
    return workflow


def _make_legacy_task(
    legacy_root: Path,
    *,
    task_id: str,
    workflow: Path,
    age: timedelta,
    progress: str = "context_store",
    supervisor: str = "supervisor",
    corrupt_event_tail: bool = False,
) -> Path:
    task_dir = legacy_root / supervisor / "20260716_120000" / "checkpoints" / task_id
    task_dir.mkdir(parents=True)
    timestamp = (NOW - age).isoformat()
    events = [
        {
            "type": "task_created",
            "task_id": task_id,
            "yaml_path": str(workflow),
            "agent_name": supervisor,
            "created_at": timestamp,
            "timestamp": timestamp,
        },
        {
            "type": "task_status_changed",
            "status": "interrupted",
            "interrupted_at": timestamp,
            "timestamp": timestamp,
        },
    ]
    event_text = "".join(json.dumps(event) + "\n" for event in events)
    if corrupt_event_tail:
        event_text += '{"type":"worker_call_started"'
    (task_dir / "task_events.jsonl").write_text(event_text, encoding="utf-8")
    _write_json(
        task_dir / "task_tree.json",
        {
            "task_id": task_id,
            "yaml_path": str(workflow),
            "agent_name": supervisor,
            "status": "interrupted",
            "created_at": timestamp,
            "interrupted_at": timestamp,
            "workers": {},
        },
    )
    _write_json(
        task_dir / "checkpoint.json",
        {
            "task_id": task_id,
            "agent_name": supervisor,
            "status": "interrupted",
            "saved_at": timestamp,
            "memory_steps": ([{"_step_type": "TaskStep", "task": "resume me"}] if progress == "memory" else []),
            "step_count": (1 if progress == "memory" else 0),
        },
    )

    if progress == "context_store":
        _write_json(
            task_dir / "context_store" / "entries" / f"{CONTEXT_REF}.json",
            {
                "ref": CONTEXT_REF,
                "kind": "text",
                "tool_name": "shell_tool",
                "original": CONTEXT_PAYLOAD,
                "preview": "preview",
                "original_chars": len(CONTEXT_PAYLOAD),
                "preview_chars": 7,
                "original_tokens_est": 10,
                "preview_tokens_est": 2,
                "strategy": "test",
                "created_at": NOW.timestamp(),
                "ttl_seconds": None,
                "source": "migration-test",
            },
        )
        (task_dir / "context_store" / "events.jsonl").write_text(
            json.dumps({"type": "compressed", "ref": CONTEXT_REF}) + "\n",
            encoding="utf-8",
        )
        _write_json(
            task_dir / "file-history" / "snapshots.json",
            {
                "first_tracked_backups": {
                    "/workspace/src/app.py": {
                        "backup_filename": "abc@v1",
                        "version": 1,
                        "backup_time": NOW.timestamp(),
                    }
                },
                "snapshots": [
                    {
                        "step_number": 1,
                        "timestamp": NOW.timestamp(),
                        "tracked_file_backups": {
                            "/workspace/src/app.py": {
                                "backup_filename": "abc@v1",
                                "version": 1,
                                "backup_time": NOW.timestamp(),
                            }
                        },
                    }
                ],
            },
        )
        (task_dir / "file-history" / "abc@v1").write_text("before edit", encoding="utf-8")

    return task_dir


def test_scan_ignores_index_and_filters_by_metadata_age_test_identity_and_real_progress(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    test_workflow = _make_workflow(repo_root, "test_app")

    valid = _make_legacy_task(
        legacy_root,
        task_id="task_valid",
        workflow=workflow,
        age=timedelta(days=1),
        corrupt_event_tail=True,
    )
    expired = _make_legacy_task(
        legacy_root,
        task_id="task_expired",
        workflow=workflow,
        age=timedelta(days=8),
    )
    _write_json(
        expired / "heartbeat.json",
        {"timestamp": NOW.timestamp(), "pid": 999999},
    )
    os.utime(expired, (NOW.timestamp(), NOW.timestamp()))
    _make_legacy_task(
        legacy_root,
        task_id="task_test",
        workflow=test_workflow,
        age=timedelta(days=1),
        supervisor="test_supervisor",
    )
    _make_legacy_task(
        legacy_root,
        task_id="task_empty",
        workflow=workflow,
        age=timedelta(days=1),
        progress="none",
    )
    _write_json(
        legacy_root / "supervisor" / ".task_index.json",
        {"task_ghost": {"run_dir": "does-not-exist", "created_at": NOW.isoformat()}},
    )

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert [candidate.task_id for candidate in plan.candidates] == ["task_valid"]
    candidate = plan.candidates[0]
    assert candidate.source_dir == valid
    assert candidate.destination_dir == runtime_root / "checkpoints" / "demo" / "task_valid"
    assert candidate.application_id == "demo"
    assert candidate.progress_kinds == ("context_store", "file_history")
    assert candidate.malformed_event_lines == 1
    assert len(candidate.checksum) == 64
    assert {item.task_id: item.reason for item in plan.skipped} == {
        "task_empty": "no resumable progress",
        "task_expired": "outside migration window",
        "task_test": "test task",
    }
    assert "task_ghost" not in {
        *(item.task_id for item in plan.skipped),
        *(item.task_id for item in plan.candidates),
    }


def test_scan_skips_legacy_task_ids_that_cannot_be_resumed_exactly(tmp_path: Path) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    workflow = _make_workflow(repo_root, "demo")
    _make_legacy_task(
        legacy_root,
        task_id="task@unsafe",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert plan.candidates == []
    assert [(item.task_id, item.reason) for item in plan.skipped] == [
        ("task@unsafe", "unsafe task id")
    ]


@pytest.mark.parametrize("symlink_ancestor", ["supervisor", "timestamp", "checkpoints"])
def test_scan_never_follows_a_symlinked_legacy_ancestor(
    tmp_path: Path,
    symlink_ancestor: str,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    external_root = tmp_path / "external-legacy"
    workflow = _make_workflow(repo_root, "demo")
    external_task = _make_legacy_task(
        external_root,
        task_id="task_external",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    external_supervisor = external_task.parents[2]
    external_timestamp = external_task.parents[1]
    external_checkpoints = external_task.parent

    if symlink_ancestor == "supervisor":
        legacy_root.mkdir(parents=True)
        (legacy_root / "supervisor").symlink_to(
            external_supervisor,
            target_is_directory=True,
        )
    elif symlink_ancestor == "timestamp":
        parent = legacy_root / "supervisor"
        parent.mkdir(parents=True)
        (parent / "20260716_120000").symlink_to(
            external_timestamp,
            target_is_directory=True,
        )
    else:
        parent = legacy_root / "supervisor" / "20260716_120000"
        parent.mkdir(parents=True)
        (parent / "checkpoints").symlink_to(
            external_checkpoints,
            target_is_directory=True,
        )

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert not plan.candidates
    assert not plan.skipped
    assert external_task.is_dir()


def test_external_workflow_uses_the_same_application_id_for_migration_and_runner(
    tmp_path: Path,
) -> None:
    from src.lib.runtime import resolve_application_id
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    workflow = tmp_path / "external_workflow.yaml"
    workflow.write_text(
        "name: custom_supervisor\n"
        "application_id: explicit-app\n"
        "description: migration test\n"
        "workflow: resume safely\n",
        encoding="utf-8",
    )
    migration = RuntimeMigration(
        legacy_logs_dir=repo_root / ".logs",
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    )

    migrated_id = migration._application_id(
        str(workflow),
        "custom_supervisor",
        repo_root / ".logs" / "legacy-task",
    )
    runner_id = resolve_application_id(
        {
            "name": "custom_supervisor",
            "application_id": "explicit-app",
            "description": "migration test",
            "workflow": "resume safely",
        },
        workflow,
        agent_root=repo_root,
    )

    assert migrated_id == runner_id == "explicit-app"


def test_scan_requires_a_parseable_live_workflow_with_matching_application_id(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"

    deleted = _make_workflow(repo_root, "deleted")
    _make_legacy_task(
        legacy_root,
        task_id="task_deleted",
        workflow=deleted,
        age=timedelta(days=1),
        progress="memory",
    )
    deleted.unlink()

    broken = _make_workflow(repo_root, "broken")
    broken.write_text("name: [unterminated\n", encoding="utf-8")
    _make_legacy_task(
        legacy_root,
        task_id="task_broken",
        workflow=broken,
        age=timedelta(days=1),
        progress="memory",
    )

    mismatched = _make_workflow(repo_root, "actual")
    mismatch_task = _make_legacy_task(
        legacy_root,
        task_id="task_mismatch",
        workflow=mismatched,
        age=timedelta(days=1),
        progress="memory",
    )
    tree = json.loads((mismatch_task / "task_tree.json").read_text(encoding="utf-8"))
    tree["application_id"] = "different"
    _write_json(mismatch_task / "task_tree.json", tree)

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert not plan.candidates
    assert {item.task_id: item.reason for item in plan.skipped} == {
        "task_broken": "invalid workflow",
        "task_deleted": "workflow file not found",
        "task_mismatch": "workflow application mismatch",
    }


def test_scan_only_tolerates_one_malformed_final_crash_tail(tmp_path: Path) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    workflow = _make_workflow(repo_root, "demo")
    middle = _make_legacy_task(
        legacy_root,
        task_id="task_middle_corruption",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    middle_lines = (middle / "task_events.jsonl").read_text(encoding="utf-8").splitlines()
    (middle / "task_events.jsonl").write_text(
        middle_lines[0] + "\n" + '{"type":"worker_call_finished"' + "\n" + middle_lines[1] + "\n",
        encoding="utf-8",
    )

    multiple = _make_legacy_task(
        legacy_root,
        task_id="task_multiple_corruption",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    with (multiple / "task_events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"type":"first_tail"\n{"type":"second_tail"')

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert not plan.candidates
    assert {item.task_id: item.reason for item in plan.skipped} == {
        "task_middle_corruption": "corrupt task events",
        "task_multiple_corruption": "corrupt task events",
    }


def test_scan_isolates_invalid_utf8_checkpoint_metadata_and_context(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    workflow = _make_workflow(repo_root, "demo")
    invalid_tree = _make_legacy_task(
        legacy_root,
        task_id="task_invalid_tree",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    (invalid_tree / "task_tree.json").write_bytes(b"\xff\xfe\x80")

    invalid_checkpoint = _make_legacy_task(
        legacy_root,
        task_id="task_invalid_checkpoint",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    (invalid_checkpoint / "checkpoint.json").write_bytes(b"\xff\xfe\x80")

    invalid_context = _make_legacy_task(
        legacy_root,
        task_id="task_invalid_context",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    bad_entry = invalid_context / "context_store" / "entries" / "ctx_bad.json"
    bad_entry.parent.mkdir(parents=True)
    bad_entry.write_bytes(b"\xff\xfe\x80")

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert not plan.candidates
    assert {item.task_id: item.reason for item in plan.skipped} == {
        "task_invalid_checkpoint": "invalid checkpoint",
        "task_invalid_context": "invalid context store",
        "task_invalid_tree": "invalid task tree",
    }


def test_invalid_worker_checkpoint_is_skipped_without_blocking_valid_task_apply(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    invalid = _make_legacy_task(
        legacy_root,
        task_id="task_invalid_worker",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    invalid_worker = invalid / "workers" / "researcher" / "checkpoint.json"
    invalid_worker.parent.mkdir(parents=True)
    invalid_worker.write_bytes(b"\xff\xfe\x80")
    _make_legacy_task(
        legacy_root,
        task_id="task_valid_worker_batch",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )

    result = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    ).migrate(dry_run=False, archive_legacy=True)

    assert [item.task_id for item in result.migrated] == ["task_valid_worker_batch"]
    assert [(item.task_id, item.reason) for item in result.plan.skipped] == [
        ("task_invalid_worker", "invalid worker checkpoint")
    ]
    assert (
        runtime_root
        / "checkpoints"
        / "demo"
        / "task_valid_worker_batch"
        / "checkpoint.json"
    ).is_file()
    assert not (
        runtime_root / "checkpoints" / "demo" / "task_invalid_worker"
    ).exists()
    assert result.archive_dir is not None
    assert not legacy_root.exists()


def test_live_legacy_heartbeat_blocks_scan_and_apply_archive(tmp_path: Path) -> None:
    from src.lib.runtime.migration import MigrationError, RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    workflow = _make_workflow(repo_root, "demo")
    task_dir = _make_legacy_task(
        legacy_root,
        task_id="task_live",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    _write_json(
        task_dir / "heartbeat.json",
        {
            "pid": os.getpid(),
            "timestamp": NOW.timestamp(),
            "status": "running",
        },
    )
    migration = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    )

    plan = migration.scan()
    assert not plan.candidates
    assert [(item.task_id, item.reason) for item in plan.skipped] == [
        ("task_live", "live legacy heartbeat")
    ]

    with pytest.raises(MigrationError, match="writer is live"):
        migration.migrate(dry_run=False, archive_legacy=True)
    assert legacy_root.is_dir()
    assert not (repo_root / ".agentloom" / "legacy").exists()


def test_scan_requires_original_created_at_instead_of_fresh_saved_metadata(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    workflow = _make_workflow(repo_root, "demo")
    task_dir = _make_legacy_task(
        legacy_root,
        task_id="task_missing_created_at",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    events = [
        json.loads(line)
        for line in (task_dir / "task_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    events[0].pop("created_at", None)
    (task_dir / "task_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    tree.pop("created_at", None)
    _write_json(task_dir / "task_tree.json", tree)

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert not plan.candidates
    assert [(item.task_id, item.reason) for item in plan.skipped] == [
        ("task_missing_created_at", "missing original created_at")
    ]


def test_scan_does_not_treat_step_count_without_memory_steps_as_progress(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    workflow = _make_workflow(repo_root, "demo")
    task_dir = _make_legacy_task(
        legacy_root,
        task_id="task_count_only",
        workflow=workflow,
        age=timedelta(days=1),
        progress="none",
    )
    checkpoint = json.loads((task_dir / "checkpoint.json").read_text(encoding="utf-8"))
    checkpoint["step_count"] = 3
    checkpoint["memory_steps"] = []
    _write_json(task_dir / "checkpoint.json", checkpoint)

    plan = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=repo_root / ".agentloom",
        agent_root=repo_root,
        now=NOW,
    ).scan()

    assert not plan.candidates
    assert [(item.task_id, item.reason) for item in plan.skipped] == [
        ("task_count_only", "no resumable progress")
    ]


def test_apply_uses_staging_verifies_context_ref_and_archives_whole_legacy_tree(
    tmp_path: Path,
) -> None:
    from src.lib.context_engine.store import ContextStore
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    task_dir = _make_legacy_task(
        legacy_root,
        task_id="task_valid",
        workflow=workflow,
        age=timedelta(days=1),
    )
    checkpoint = json.loads((task_dir / "checkpoint.json").read_text(encoding="utf-8"))
    checkpoint.update(
        {
            "memory_steps": [{"_step_type": "TaskStep", "task": "resume supervisor"}],
            "step_count": 1,
        }
    )
    _write_json(task_dir / "checkpoint.json", checkpoint)
    tree = json.loads((task_dir / "task_tree.json").read_text(encoding="utf-8"))
    tree["workers"] = {
        "researcher": [
            {
                "call_index": 2,
                "status": "interrupted",
                "input_hash": "hash-2",
            }
        ]
    }
    _write_json(task_dir / "task_tree.json", tree)
    _write_json(
        task_dir / "workers" / "researcher" / "checkpoint.json",
        {
            "task_id": "task_valid",
            "agent_name": "researcher",
            "agent_type": "worker",
            "call_index": 2,
            "status": "interrupted",
            "saved_at": (NOW - timedelta(days=1)).isoformat(),
            "memory_steps": [{"_step_type": "TaskStep", "task": "resume worker"}],
            "step_count": 1,
        },
    )
    (legacy_root / "unrelated-runtime.log").write_text("archive me", encoding="utf-8")
    stale_stage = runtime_root / ".migration-staging" / "abandoned" / "partial"
    stale_stage.mkdir(parents=True)
    (stale_stage / "checkpoint.json.tmp").write_text("partial", encoding="utf-8")
    migration = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    )

    dry_run = migration.migrate(dry_run=True, archive_legacy=True)
    assert dry_run.dry_run is True
    assert not (runtime_root / "checkpoints").exists()
    assert legacy_root.exists()

    validated: list[str] = []

    def validate(candidate, destination: Path) -> None:
        assert ".migration-staging" in destination.parts
        assert not candidate.destination_dir.exists()
        store = ContextStore(destination / "context_store")
        assert store.retrieve(CONTEXT_REF) == CONTEXT_PAYLOAD
        assert (destination / "file-history" / "abc@v1").read_text(encoding="utf-8") == "before edit"
        validated.append(candidate.task_id)

    result = migration.migrate(
        dry_run=False,
        archive_legacy=True,
        validator=validate,
    )

    destination = runtime_root / "checkpoints" / "demo" / "task_valid"
    assert result.migrated_count == 1
    assert validated == ["task_valid"]
    assert destination.is_dir()
    assert not (destination / "workers" / "researcher" / "checkpoint.json").exists()
    worker_checkpoint = destination / "workers" / "researcher" / "calls" / "2" / "checkpoint.json"
    assert worker_checkpoint.is_file()
    from src.lib.checkpoint import CheckpointManager

    manager = CheckpointManager("supervisor", checkpoint_dir=destination)
    assert manager.load_worker_checkpoint("task_valid", "researcher", 2)["step_count"] == 1
    assert ContextStore(destination / "context_store").retrieve(CONTEXT_REF) == CONTEXT_PAYLOAD
    assert not legacy_root.exists()
    assert result.archive_dir is not None
    assert result.archive_dir.parent == runtime_root / "legacy"
    assert result.archive_dir.name.startswith("logs-v1-20260716T120000")
    assert (result.archive_dir / "unrelated-runtime.log").read_text(encoding="utf-8") == "archive me"
    assert not (runtime_root / ".migration-staging").exists()
    assert not (runtime_root / ".migration.lock").exists()
    assert not (runtime_root / "runs").exists()


def test_archive_failure_keeps_published_task_leased_until_safe_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.lib.runtime.migration as migration_module
    from src.lib.checkpoint.checkpoint_manager import CheckpointTaskLease
    from src.lib.runtime.migration import MigrationError, RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    _make_legacy_task(
        legacy_root,
        task_id="task_valid",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    migration = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    )
    archive_entered = threading.Event()
    allow_archive_failure = threading.Event()

    def fail_archive(*_args, **_kwargs):
        archive_entered.set()
        assert allow_archive_failure.wait(timeout=5)
        raise OSError("archive failed")

    monkeypatch.setattr(migration_module, "archive_legacy_logs", fail_archive)
    errors: list[BaseException] = []

    def apply_migration() -> None:
        try:
            migration.migrate(dry_run=False, archive_legacy=True)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=apply_migration)
    thread.start()
    assert archive_entered.wait(timeout=5)

    destination = runtime_root / "checkpoints" / "demo" / "task_valid"
    assert destination.is_dir()
    with pytest.raises(RuntimeError, match="already active"):
        CheckpointTaskLease(destination, require_exists=True).acquire()

    allow_archive_failure.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], MigrationError)
    assert "archive failed" in str(errors[0])
    assert legacy_root.is_dir()
    assert not destination.exists()


def test_source_change_during_staging_aborts_before_publish_or_archive(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import MigrationError, RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    source = _make_legacy_task(
        legacy_root,
        task_id="task_changes",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    migration = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    )

    def append_legacy_progress(_candidate, _staged: Path) -> None:
        checkpoint_path = source / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["memory_steps"].append(
            {"_step_type": "TaskStep", "task": "new legacy progress"}
        )
        checkpoint["step_count"] = 2
        _write_json(checkpoint_path, checkpoint)

    with pytest.raises(MigrationError, match="changed during migration"):
        migration.migrate(
            dry_run=False,
            archive_legacy=True,
            validator=append_legacy_progress,
        )

    assert legacy_root.is_dir()
    assert not (runtime_root / "checkpoints" / "demo" / "task_changes").exists()
    assert not (runtime_root / "legacy").exists()


def test_apply_is_idempotent_when_destination_already_matches(tmp_path: Path) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    _make_legacy_task(
        legacy_root,
        task_id="task_valid",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    migration = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    )

    first = migration.migrate(dry_run=False, archive_legacy=False)
    second = migration.migrate(dry_run=False, archive_legacy=False)

    assert first.migrated_count == 1
    assert second.migrated_count == 0
    assert second.already_migrated_count == 1


def test_validator_failure_rolls_back_destination_and_keeps_legacy_tree(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import MigrationError, RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    _make_legacy_task(
        legacy_root,
        task_id="task_valid",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    migration = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    )

    def reject(_candidate, _destination: Path) -> None:
        raise RuntimeError("resume validation failed")

    with pytest.raises(MigrationError, match="resume validation failed"):
        migration.migrate(
            dry_run=False,
            archive_legacy=True,
            validator=reject,
        )

    assert legacy_root.exists()
    assert not (runtime_root / "checkpoints" / "demo" / "task_valid").exists()
    assert not (runtime_root / ".migration-staging").exists()
    assert not (runtime_root / ".migration.lock").exists()


def test_apply_rejects_symlinked_destination_component_and_keeps_legacy(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.migration import MigrationError, RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    _make_legacy_task(
        legacy_root,
        task_id="task_valid",
        workflow=workflow,
        age=timedelta(days=1),
        progress="memory",
    )
    external = tmp_path / "external"
    external.mkdir()
    checkpoints = runtime_root / "checkpoints"
    checkpoints.mkdir(parents=True)
    (checkpoints / "demo").symlink_to(external, target_is_directory=True)

    with pytest.raises(MigrationError, match="symlink"):
        RuntimeMigration(
            legacy_logs_dir=legacy_root,
            runtime_root=runtime_root,
            agent_root=repo_root,
            now=NOW,
        ).migrate(dry_run=False, archive_legacy=True)

    assert legacy_root.exists()
    assert list(external.iterdir()) == []


def test_scan_skips_checkpoint_with_missing_file_history_backup(tmp_path: Path) -> None:
    from src.lib.runtime.migration import RuntimeMigration

    repo_root = tmp_path / "repo"
    legacy_root = repo_root / ".logs"
    runtime_root = repo_root / ".agentloom"
    workflow = _make_workflow(repo_root, "demo")
    task_dir = _make_legacy_task(
        legacy_root,
        task_id="task_invalid_history",
        workflow=workflow,
        age=timedelta(days=1),
    )
    (task_dir / "file-history" / "abc@v1").unlink()

    result = RuntimeMigration(
        legacy_logs_dir=legacy_root,
        runtime_root=runtime_root,
        agent_root=repo_root,
        now=NOW,
    ).migrate(dry_run=False, archive_legacy=True)

    assert result.migrated_count == 0
    assert {item.task_id: item.reason for item in result.plan.skipped} == {
        "task_invalid_history": "invalid file history"
    }
    assert not (runtime_root / "checkpoints" / "demo" / "task_invalid_history").exists()
