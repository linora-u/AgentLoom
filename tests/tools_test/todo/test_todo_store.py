"""Persistent Todo state tests at the checkpoint task boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from src.lib.checkpoint.checkpoint_manager import CheckpointManager


def _manager(tmp_path, task_id: str = "task") -> CheckpointManager:
    return CheckpointManager(
        "supervisor",
        checkpoint_dir=tmp_path / "checkpoints" / "app" / task_id,
        run_id="run",
    )


def test_checkpoint_store_round_trip_and_agent_isolation(tmp_path) -> None:
    manager = _manager(tmp_path)

    supervisor = manager.replace_todos(
        "task",
        "supervisor",
        [{"content": "Delegate work", "status": "in_progress"}],
    )
    worker = manager.replace_todos(
        "task",
        "supervisor/worker",
        [{"content": "Inspect files", "status": "pending"}],
    )

    assert supervisor["revision"] == 1
    assert worker["revision"] == 1
    assert manager.load_todos("task", "supervisor")["items"][0]["content"] == "Delegate work"
    assert manager.load_todos("task", "supervisor/worker")["items"][0]["content"] == "Inspect files"

    document = json.loads((manager.checkpoint_dir / "todos.json").read_text())
    assert document["schema_version"] == 1
    assert document["task_id"] == "task"
    assert "revision" not in document
    assert set(document["agents"]) == {"supervisor", "supervisor/worker"}


def test_reopening_manager_restores_snapshot(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.replace_todos(
        "task",
        "supervisor",
        [{"content": "Persist me", "status": "pending"}],
    )
    manager.close()

    reopened = _manager(tmp_path)

    assert reopened.load_todos("task", "supervisor") == {
        "revision": 1,
        "items": [{"content": "Persist me", "status": "pending"}],
        "corrupt": False,
    }


def test_corrupt_document_is_quarantined_and_returns_empty(tmp_path, caplog) -> None:
    manager = _manager(tmp_path)
    storage = manager.task_storage("task")
    try:
        storage.atomic_write_text("todos.json", "{not-json")
    finally:
        storage.close()

    snapshot = manager.load_todos("task", "supervisor")

    assert snapshot == {"revision": 0, "items": [], "corrupt": True}
    assert not (manager.checkpoint_dir / "todos.json").exists()
    assert list(manager.checkpoint_dir.glob("todos.corrupt.*.json"))
    assert "corrupt Todo state" in caplog.text
    assert "task_id=task" in caplog.text
    assert "agent_path=supervisor" in caplog.text
    assert manager.load_todos("task", "supervisor")["corrupt"] is True


def test_invalid_replace_does_not_change_persisted_snapshot(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.replace_todos(
        "task",
        "supervisor",
        [{"content": "Existing", "status": "pending"}],
    )

    try:
        manager.replace_todos(
            "task",
            "supervisor",
            [
                {"content": "One", "status": "in_progress"},
                {"content": "Two", "status": "in_progress"},
            ],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid Todo replacement unexpectedly succeeded")

    assert manager.load_todos("task", "supervisor")["items"] == [{"content": "Existing", "status": "pending"}]


def test_concurrent_managers_preserve_agent_scopes_and_valid_document(tmp_path) -> None:
    first = _manager(tmp_path)
    second = _manager(tmp_path)

    def replace(manager: CheckpointManager, agent_path: str, content: str) -> None:
        manager.replace_todos(
            "task",
            agent_path,
            [{"content": content, "status": "pending"}],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(replace, first, "supervisor", "Parent work"),
            executor.submit(replace, second, "supervisor/worker", "Child work"),
        ]
        for future in futures:
            future.result()

    document = json.loads((first.checkpoint_dir / "todos.json").read_text())
    assert set(document["agents"]) == {"supervisor", "supervisor/worker"}
    assert first.load_todos("task", "supervisor")["items"][0]["content"] == "Parent work"
    assert second.load_todos("task", "supervisor/worker")["items"][0]["content"] == "Child work"


def test_recovery_write_keeps_quarantined_evidence(tmp_path) -> None:
    manager = _manager(tmp_path)
    storage = manager.task_storage("task")
    try:
        storage.atomic_write_text("todos.json", "{broken")
    finally:
        storage.close()

    assert manager.load_todos("task", "supervisor")["corrupt"] is True
    quarantined = list(manager.checkpoint_dir.glob("todos.corrupt.*.json"))
    manager.replace_todos(
        "task",
        "supervisor",
        [{"content": "Recovered", "status": "pending"}],
    )

    assert quarantined and all(path.exists() for path in quarantined)
    assert manager.load_todos("task", "supervisor") == {
        "revision": 1,
        "items": [{"content": "Recovered", "status": "pending"}],
        "corrupt": False,
    }


def test_replace_does_not_overwrite_corrupt_evidence_when_quarantine_fails(
    tmp_path,
    monkeypatch,
) -> None:
    from src.lib.runtime import SecureDirectory

    manager = _manager(tmp_path)
    storage = manager.task_storage("task")
    try:
        storage.atomic_write_text("todos.json", "{broken")
    finally:
        storage.close()

    def fail_rename(self, source, destination):
        raise OSError("quarantine unavailable")

    monkeypatch.setattr(SecureDirectory, "rename_file", fail_rename)

    try:
        manager.replace_todos(
            "task",
            "supervisor",
            [{"content": "Must not overwrite", "status": "pending"}],
        )
    except RuntimeError as exc:
        assert "diagnostic evidence" in str(exc)
    else:
        raise AssertionError("replace unexpectedly overwrote corrupt Todo evidence")

    assert (manager.checkpoint_dir / "todos.json").read_text() == "{broken"
