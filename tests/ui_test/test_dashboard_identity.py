from src.lib.checkpoint.checkpoint_manager import CheckpointManager
from src.ui.dashboard import _delete_dashboard_task, _find_task, _task_row_key


def test_dashboard_row_key_includes_application_identity() -> None:
    assert _task_row_key("alpha", "task-1") != _task_row_key("beta", "task-1")


def test_dashboard_task_lookup_uses_application_and_task_id() -> None:
    tasks = [
        {"application_id": "alpha", "task_id": "same", "checkpoint_dir": "/alpha"},
        {"application_id": "beta", "task_id": "same", "checkpoint_dir": "/beta"},
    ]

    assert _find_task(tasks, ("beta", "same")) == tasks[1]


def test_dashboard_delete_preserves_task_with_active_run_lease(tmp_path) -> None:
    task_dir = tmp_path / "checkpoints" / "app" / "task"
    manager = CheckpointManager("agent", checkpoint_dir=task_dir)
    manager.save_task_tree(
        "task",
        {
            "task_id": "task",
            "agent_name": "agent",
            "status": "interrupted",
            "created_at": "2026-07-16T00:00:00+00:00",
        },
    )

    with manager.task_lease():
        deleted = _delete_dashboard_task({"checkpoint_dir": str(task_dir)})

    assert deleted is False
    assert task_dir.exists()
