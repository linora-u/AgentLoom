"""Canonical runtime-path contracts for checkpoint persistence."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from src.lib.checkpoint.checkpoint_manager import (
    CheckpointManager,
    cleanup_expired_tasks,
    list_all_tasks,
)
from src.lib.checkpoint.coordinator import CheckpointCoordinator
from src.lib.runtime import RuntimeHome


def _context(tmp_path, *, task_id: str = "task_123", run_id: str = "run_1"):
    return RuntimeHome(tmp_path / ".agentloom").context(
        application_id="tools/search",
        task_id=task_id,
        run_id=run_id,
    )


def test_checkpoint_manager_writes_only_to_canonical_task_directory(tmp_path):
    context = _context(tmp_path)
    manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )

    manager.save_task_tree(
        context.task_id,
        {"task_id": context.task_id, "status": "running", "workers": {}},
    )

    assert context.task_tree_path.exists()
    assert context.task_events_path.exists()
    assert not list((tmp_path / ".agentloom").rglob(".task_index.json"))


def test_resume_event_keeps_task_directory_and_records_new_run_id(tmp_path):
    first = _context(tmp_path, run_id="run_first")
    first_manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=first.checkpoint_dir,
        run_id=first.run_id,
    )
    first_manager.record_task_created(
        first.task_id,
        yaml_path="applications/tools/search/workflows/agent.yaml",
        agent_name="search_supervisor",
        task_text="search",
        created_at="2026-07-16T10:00:00+08:00",
    )
    first_manager.record_run_started(first.task_id)

    resumed = _context(tmp_path, run_id="run_resumed")
    resumed_manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=resumed.checkpoint_dir,
        run_id=resumed.run_id,
    )
    resumed_manager.record_run_resumed(resumed.task_id)

    events = [json.loads(line) for line in resumed.task_events_path.read_text(encoding="utf-8").splitlines()]
    assert resumed.checkpoint_dir == first.checkpoint_dir
    assert [(event["type"], event.get("run_id")) for event in events[-2:]] == [
        ("run_started", "run_first"),
        ("run_resumed", "run_resumed"),
    ]
    assert resumed_manager.load_task_tree(resumed.task_id)["run_id"] == "run_resumed"


def test_checkpoint_payload_records_the_writing_run_id(tmp_path):
    context = _context(tmp_path, run_id="run_writer")
    manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )

    manager.save_supervisor_checkpoint(
        context.task_id,
        memory_steps=[],
        task_text="search",
        status="interrupted",
    )

    checkpoint = json.loads(context.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["run_id"] == "run_writer"


def test_task_tree_projection_replacement_preserves_run_history(tmp_path):
    context = _context(tmp_path, run_id="run_writer")
    manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )
    manager.record_task_created(
        context.task_id,
        yaml_path="agent.yaml",
        agent_name="search_supervisor",
        task_text="search",
        created_at="2026-07-16T10:00:00+08:00",
    )
    manager.record_run_started(context.task_id)

    manager.save_task_tree(
        context.task_id,
        {"task_id": context.task_id, "status": "interrupted", "workers": {}},
    )

    tree = manager.load_task_tree(context.task_id)
    assert tree["run_id"] == "run_writer"
    assert tree["runs"] == [
        {
            "run_id": "run_writer",
            "event": "run_started",
            "started_at": tree["runs"][0]["started_at"],
        }
    ]


def test_list_all_tasks_scans_canonical_tree_without_indexes(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    contexts = [
        runtime.context(application_id="alpha", task_id="task_a", run_id="run_a"),
        runtime.context(application_id="nested/beta", task_id="task_b", run_id="run_b"),
    ]
    for context in contexts:
        manager = CheckpointManager(
            "supervisor",
            checkpoint_dir=context.checkpoint_dir,
            run_id=context.run_id,
        )
        manager.save_task_tree(
            context.task_id,
            {
                "task_id": context.task_id,
                "agent_name": context.application_id,
                "status": "interrupted",
                "workers": {},
            },
        )

    tasks = list_all_tasks(checkpoints_root=runtime.root_dir / "checkpoints")

    assert {task["task_id"] for task in tasks} == {"task_a", "task_b"}
    assert {task["application_id"] for task in tasks} == {"alpha", "nested/beta"}


def test_list_all_tasks_does_not_treat_worker_checkpoints_as_tasks(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(application_id="alpha", task_id="task_a", run_id="run_a")
    manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "agent_name": "supervisor",
            "status": "interrupted",
            "workers": {},
        },
    )
    manager.save_worker_checkpoint(
        context.task_id,
        "worker_a",
        call_index=0,
        status="interrupted",
    )

    tasks = list_all_tasks(checkpoints_root=runtime.root_dir / "checkpoints")

    assert [(task["application_id"], task["task_id"]) for task in tasks] == [
        ("alpha", "task_a")
    ]


def test_list_all_tasks_rejects_symlinked_checkpoint_root(tmp_path):
    external_root = tmp_path / "external"
    manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=external_root / "app" / "task",
        run_id="run",
    )
    manager.save_task_tree(
        "task",
        {"task_id": "task", "status": "interrupted", "workers": {}},
    )
    runtime_root = tmp_path / ".agentloom"
    runtime_root.mkdir()
    (runtime_root / "checkpoints").symlink_to(external_root, target_is_directory=True)

    assert list_all_tasks(checkpoints_root=runtime_root / "checkpoints") == []
    assert (external_root / "app" / "task" / "task_tree.json").exists()


def test_prepare_checkpoint_rejects_symlinked_application_component(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    external = tmp_path / "external"
    external.mkdir()
    checkpoints = runtime.root_dir / "checkpoints"
    checkpoints.mkdir(parents=True)
    (checkpoints / "app").symlink_to(external, target_is_directory=True)
    context = runtime.context(application_id="app", task_id="task", run_id="run")

    with pytest.raises(RuntimeError, match="symlink"):
        context.prepare_checkpoint()

    assert not (external / "task").exists()


@pytest.mark.parametrize("worker_name", ["../escaped", "../../../../escaped", "bad\\name"])
def test_worker_checkpoint_paths_reject_unsafe_worker_names(tmp_path, worker_name):
    context = _context(tmp_path)
    manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )

    with pytest.raises(ValueError, match="worker_name"):
        manager.save_worker_checkpoint(
            context.task_id,
            worker_name,
            call_index=0,
            status="interrupted",
        )


def test_worker_heartbeat_path_rejects_unsafe_worker_name(tmp_path):
    context = _context(tmp_path)
    manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )
    coordinator = CheckpointCoordinator(
        manager,
        context.task_id,
        "search",
    )

    try:
        with pytest.raises(ValueError, match="worker_name"):
            coordinator.prepare_worker_call(
                "../../../../escaped",
                input_hash="hash",
                task_input="unsafe",
            )
    finally:
        coordinator.stop_all_worker_heartbeats()


def test_deactivate_stops_worker_heartbeats_before_clearing_context(tmp_path):
    context = _context(tmp_path)
    manager = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )
    coordinator = CheckpointCoordinator.activate(
        manager,
        context.task_id,
        "search",
    )

    class _Heartbeat:
        stopped = False

        def stop(self):
            self.stopped = True

    heartbeat = _Heartbeat()
    coordinator._worker_heartbeats["worker"] = heartbeat

    CheckpointCoordinator.deactivate(coordinator)

    assert heartbeat.stopped is True
    assert coordinator._worker_heartbeats == {}
    assert CheckpointCoordinator.current() is None


def test_context_engine_uses_effective_application_config(tmp_path):
    first = _context(tmp_path, task_id="task_first", run_id="run_first")
    first_manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=first.checkpoint_dir,
        run_id=first.run_id,
    )
    first_coord = CheckpointCoordinator.activate(
        first_manager,
        first.task_id,
        "task",
        effective_config={
            "context_engine": {
                "min_chars": 111,
                "store": {"max_entries": 7},
            },
            "checkpoint": {"max_resume_age": 30 * 86400},
        },
    )
    try:
        assert first_coord._context_engine.config.min_chars == 111
        assert first_coord._context_engine.config.store.max_entries == 7
        assert first_coord._context_engine.config.store.ttl_seconds == 30 * 86400
    finally:
        CheckpointCoordinator.deactivate(first_coord)

    second = _context(tmp_path, task_id="task_second", run_id="run_second")
    second_manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=second.checkpoint_dir,
        run_id=second.run_id,
    )
    second_coord = CheckpointCoordinator.activate(
        second_manager,
        second.task_id,
        "task",
        effective_config={
            "context_engine": {
                "min_chars": 222,
                "store": {"ttl_seconds": 45},
            },
            "checkpoint": {"max_resume_age": 60 * 86400},
        },
    )
    try:
        assert second_coord._context_engine.config.min_chars == 222
        assert second_coord._context_engine.config.store.ttl_seconds == 45
    finally:
        CheckpointCoordinator.deactivate(second_coord)


def test_task_lease_rejects_concurrent_attempt_for_same_task(tmp_path):
    context = _context(tmp_path)
    first = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id="run_first",
    )
    second = CheckpointManager(
        "search_supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id="run_second",
    )

    with first.task_lease():
        with pytest.raises(RuntimeError, match="already active"):
            with second.task_lease():
                pass

    with second.task_lease():
        second.record_task_created(
            context.task_id,
            yaml_path="applications/tools/search/workflows/agent.yaml",
            agent_name="search_supervisor",
            task_text="search",
            created_at="2026-07-16T10:00:00+08:00",
        )


def test_resume_task_lease_does_not_recreate_a_missing_checkpoint(tmp_path):
    checkpoint_dir = tmp_path / "checkpoints" / "app" / "missing-task"
    manager = CheckpointManager("agent", checkpoint_dir=checkpoint_dir)

    with pytest.raises(FileNotFoundError):
        manager.task_lease(require_exists=True).acquire()

    assert not checkpoint_dir.exists()


def test_checkpoint_writes_stay_on_leased_inode_after_task_path_replacement(
    tmp_path,
):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    first = runtime.context(
        application_id="app",
        task_id="task_first",
        run_id="run_first",
    )
    second = runtime.context(
        application_id="app",
        task_id="task_second",
        run_id="run_second",
    )
    first.prepare_checkpoint()
    second.prepare_checkpoint()
    first_manager = CheckpointManager("first", checkpoint_dir=first.checkpoint_dir)
    second_manager = CheckpointManager("second", checkpoint_dir=second.checkpoint_dir)
    second_manager.save_supervisor_checkpoint(
        second.task_id,
        memory_steps=[],
        task_text="second",
        status="failed",
    )
    detached = first.checkpoint_dir.parent / "task_first_detached"

    with first_manager.task_lease():
        first.checkpoint_dir.rename(detached)
        first.checkpoint_dir.symlink_to(second.checkpoint_dir, target_is_directory=True)
        first_manager.save_supervisor_checkpoint(
            first.task_id,
            memory_steps=[],
            task_text="FIRST-SECRET",
            status="interrupted",
        )

    second_payload = json.loads(second.checkpoint_path.read_text(encoding="utf-8"))
    first_payload = json.loads((detached / "checkpoint.json").read_text(encoding="utf-8"))
    assert second_payload["task_text"] == "second"
    assert first_payload["task_text"] == "FIRST-SECRET"


def test_expired_cleanup_preserves_task_with_active_attempt_lease(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(
        application_id="alpha",
        task_id="task_active",
        run_id="run_active",
    )
    manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "status": "failed",
            "created_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
            "workers": {},
        },
    )

    with manager.task_lease():
        removed = cleanup_expired_tasks(
            checkpoints_root=runtime.root_dir / "checkpoints",
            max_age_seconds=7 * 86400,
        )

    assert removed == 0
    assert context.checkpoint_dir.exists()


def test_cleanup_expired_tasks_uses_logical_age_and_preserves_live_task(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    contexts = {
        "expired": runtime.context(application_id="app", task_id="expired", run_id="run_old"),
        "fresh": runtime.context(application_id="app", task_id="fresh", run_id="run_new"),
        "live": runtime.context(application_id="app", task_id="live", run_id="run_live"),
    }
    for name, context in contexts.items():
        manager = CheckpointManager(
            "supervisor",
            checkpoint_dir=context.checkpoint_dir,
            run_id=context.run_id,
        )
        manager.save_task_tree(
            context.task_id,
            {
                "task_id": context.task_id,
                "status": "running" if name == "live" else "failed",
                "created_at": fresh if name == "fresh" else old,
                "workers": {},
            },
        )
    contexts["live"].heartbeat_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "timestamp": time.time(),
                "status": "running",
                "run_id": contexts["live"].run_id,
            }
        ),
        encoding="utf-8",
    )

    removed = cleanup_expired_tasks(
        checkpoints_root=runtime.root_dir / "checkpoints",
        max_age_seconds=7 * 86400,
    )

    assert removed == 1
    assert not contexts["expired"].checkpoint_dir.exists()
    assert contexts["fresh"].checkpoint_dir.exists()
    assert contexts["live"].checkpoint_dir.exists()


def test_application_cleanup_does_not_recurse_into_nested_application(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    now = datetime.now(UTC)
    contexts = [
        runtime.context(application_id="foo", task_id="task_foo", run_id="run_foo"),
        runtime.context(
            application_id="foo/bar",
            task_id="task_bar",
            run_id="run_bar",
        ),
    ]
    for context in contexts:
        manager = CheckpointManager(
            "supervisor",
            checkpoint_dir=context.checkpoint_dir,
            run_id=context.run_id,
        )
        manager.save_task_tree(
            context.task_id,
            {
                "task_id": context.task_id,
                "status": "failed",
                "created_at": (now - timedelta(days=2)).isoformat(),
                "workers": {},
            },
        )

    removed = cleanup_expired_tasks(
        checkpoints_root=contexts[0].checkpoint_dir.parent,
        max_age_seconds=86400,
        now=now,
        recursive=False,
    )

    assert removed == 1
    assert not contexts[0].checkpoint_dir.exists()
    assert contexts[1].checkpoint_dir.exists()


def test_cleanup_expired_tasks_preserves_terminal_task_with_live_heartbeat(tmp_path):
    """The heartbeat is authoritative even if the task projection lags behind."""
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(application_id="app", task_id="active", run_id="run_active")
    manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=context.checkpoint_dir,
        run_id=context.run_id,
    )
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "status": "failed",
            "created_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
            "workers": {},
        },
    )
    context.heartbeat_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "timestamp": time.time(),
                "status": "running",
                "run_id": context.run_id,
            }
        ),
        encoding="utf-8",
    )

    removed = cleanup_expired_tasks(
        checkpoints_root=runtime.root_dir / "checkpoints",
        max_age_seconds=7 * 86400,
    )

    assert removed == 0
    assert context.checkpoint_dir.exists()


@pytest.mark.parametrize("max_age_seconds", [0, -1])
def test_cleanup_expired_tasks_non_positive_ttl_disables_cleanup(
    tmp_path,
    max_age_seconds,
):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(application_id="app", task_id="keep", run_id="run_keep")
    manager = CheckpointManager("supervisor", checkpoint_dir=context.checkpoint_dir)
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "status": "failed",
            "created_at": (datetime.now(UTC) - timedelta(days=365)).isoformat(),
            "workers": {},
        },
    )

    removed = cleanup_expired_tasks(
        checkpoints_root=runtime.root_dir / "checkpoints",
        max_age_seconds=max_age_seconds,
    )

    assert removed == 0
    assert context.checkpoint_dir.exists()


def test_cleanup_expired_tasks_accepts_one_crash_truncated_event_tail(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(application_id="app", task_id="damaged", run_id="run_damaged")
    manager = CheckpointManager("supervisor", checkpoint_dir=context.checkpoint_dir)
    manager.record_task_created(
        context.task_id,
        yaml_path="agent.yaml",
        agent_name="supervisor",
        task_text="task",
        created_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(),
    )
    manager.record_task_status_changed(context.task_id, "failed")
    with context.task_events_path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "run_resumed"')

    removed = cleanup_expired_tasks(
        checkpoints_root=runtime.root_dir / "checkpoints",
        max_age_seconds=7 * 86400,
    )

    assert removed == 1
    assert not context.checkpoint_dir.exists()


def test_cleanup_expired_tasks_ignores_stale_framework_temp_files(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(
        application_id="app",
        task_id="stale-temp",
        run_id="run",
    )
    manager = CheckpointManager("agent", checkpoint_dir=context.checkpoint_dir)
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "agent_name": "agent",
            "status": "failed",
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    )
    (context.checkpoint_dir / "checkpoint.dead.tmp").write_text("partial")

    removed = cleanup_expired_tasks(
        checkpoints_root=runtime.checkpoints_root,
        max_age_seconds=7 * 86400,
        now=datetime(2026, 7, 16, tzinfo=UTC),
    )

    assert removed == 1
    assert not context.checkpoint_dir.exists()


def test_cleanup_expired_tasks_never_uses_checkpoint_saved_at_as_task_age(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(application_id="app", task_id="no_anchor", run_id="run_old")
    context.checkpoint_dir.mkdir(parents=True)
    context.checkpoint_path.write_text(
        json.dumps(
            {
                "task_id": context.task_id,
                "agent_name": "supervisor",
                "status": "failed",
                "saved_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    removed = cleanup_expired_tasks(
        checkpoints_root=runtime.root_dir / "checkpoints",
        max_age_seconds=7 * 86400,
    )

    assert removed == 0
    assert context.checkpoint_dir.exists()


@pytest.mark.parametrize("max_age_seconds", [float("nan"), float("inf")])
def test_cleanup_expired_tasks_rejects_non_finite_ttl(tmp_path, max_age_seconds):
    with pytest.raises(ValueError, match="finite"):
        cleanup_expired_tasks(
            checkpoints_root=tmp_path / "checkpoints",
            max_age_seconds=max_age_seconds,
        )


def test_cleanup_expired_tasks_skips_when_another_cleaner_holds_root_lock(tmp_path):
    runtime = RuntimeHome(tmp_path / ".agentloom")
    context = runtime.context(application_id="app", task_id="expired", run_id="run_old")
    manager = CheckpointManager("supervisor", checkpoint_dir=context.checkpoint_dir)
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "status": "failed",
            "created_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
            "workers": {},
        },
    )

    checkpoints_root = runtime.root_dir / "checkpoints"
    lock_fd = os.open(checkpoints_root, os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        removed = cleanup_expired_tasks(
            checkpoints_root=checkpoints_root,
            max_age_seconds=7 * 86400,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert removed == 0
    assert context.checkpoint_dir.exists()
