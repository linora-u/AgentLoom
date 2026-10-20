"""Public Application submissions using the real DBOS SQLite client."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.durable import QueueDeclarationConflict, TaskInputConflict
from jsonschema import ValidationError

from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project


def test_known_sqlite_wal_deadlock_is_rejected_before_queue_storage(batch_project, monkeypatch):
    import sqlite3

    root, application, _, probe, _ = batch_project
    namespace = root / "persistent"
    with Application.open(application, project_root=root, file_logging=False) as app:
        with monkeypatch.context() as library:
            library.setattr(sqlite3, "sqlite_version_info", (3, 51, 1))
            with pytest.raises(RuntimeError, match="SQLite 3.51.1.*WAL.*3.51.2"):
                app.durable(root=namespace)
    assert not (namespace / "system.sqlite").exists()
    assert not (namespace / "namespace.json").exists()
    assert probe.starts == []


def test_durable_queue_keeps_persisted_and_live_capacity_equal(batch_project):
    root, application, _, _, write = batch_project
    write(application / "config/system.yaml", {"worker_execution": {"defaults": {"concurrency": 2}}})
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=root / "persistent") as execution:
        worker = execution.worker("workflows/worker.yaml")
        worker.submit(WorkItem("one", {"value": 1}))
        queue = execution.client.retrieve_queue(worker.queue_name)
        assert queue.worker_concurrency == queue.global_concurrency == 2


def test_queued_work_has_no_physical_attempt_before_consumer_admission(batch_project):
    root, application, _, _, _ = batch_project
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=root / "persistent") as execution:
        handle = execution.worker("workflows/worker.yaml").submit(WorkItem("queued", {"value": 1}))
        assert execution.status(handle.workflow_id).status == "ENQUEUED"
        assert execution.resource_status(handle.workflow_id) == []


def test_stopped_namespace_upgrades_legacy_queue_without_losing_work(batch_project):
    root, application, _, _, _ = batch_project
    namespace = root / "persistent"
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        handle = worker.submit(WorkItem("preserved", {"value": 1}))
        execution.client.retrieve_queue(worker.queue_name).set_global_concurrency(None)
        assert execution.queues_need_migration(["workflows/worker.yaml"])
        execution.migrate_queues(["workflows/worker.yaml"])
        assert not execution.queues_need_migration(["workflows/worker.yaml"])
        assert execution.worker("workflows/worker.yaml").queue_name == worker.queue_name
        assert execution.status(handle.workflow_id).status == "ENQUEUED"


def test_unsupported_process_host_rejects_before_durable_consumption(batch_project, monkeypatch):
    from agentloom.execution.durable.os_job import ResourceOwnershipError
    root, application, _, _, _ = batch_project
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=root / "persistent") as execution:
        handle = execution.worker("workflows/worker.yaml").submit(WorkItem("queued", {"value": 1}))
        executor = execution.executor(["workflows/worker.yaml"])
        with monkeypatch.context() as platform:
            platform.setattr(sys, "platform", "unsupported")
            with pytest.raises(ResourceOwnershipError, match="macOS or Linux"):
                executor.run(idle_timeout=0.1)
        assert execution.status(handle.workflow_id).status == "ENQUEUED"
        assert execution.resource_status(handle.workflow_id) == []


def test_durable_identity_input_snapshot_and_atomic_conflict(batch_project):
    root, application, _, probe, _ = batch_project
    namespace_root = root / "persistent"
    original = {"value": [1]}
    item = WorkItem("stable", original)
    original["value"].append(2)
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace_root) as execution:
        worker = execution.worker("workflows/worker.yaml")
        with pytest.raises(ValidationError):
            worker.submit(WorkItem("invalid", {"bad": 1}))
        first = worker.submit_many([item, WorkItem("other", {"value": 3})])
        assert len(first) == 2
        rows = execution.client.list_workflows(application_name=execution.namespace, load_input=True)
        assert len(rows) == 2 and all(row.status == "ENQUEUED" for row in rows)
        task = worker.description(item)
        assert next(row for row in rows if row.workflow_id == task.workflow_id).input["args"][0]["item"]["input"] == {"value": [1]}
        with pytest.raises(TaskInputConflict):
            worker.submit_many([WorkItem("new-before-conflict", {"value": 4}), WorkItem("stable", {"value": [9]})])
        assert len(execution.client.list_workflows(application_name=execution.namespace)) == 2
        original_run = app.run.run_id
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace_root) as execution:
        worker = execution.worker("workflows/worker.yaml")
        worker.submit(item)
        assert app.run.run_id != original_run
        assert len(execution.client.list_workflows(application_name=execution.namespace)) == 2
        assert worker.description(item).workflow_id == task.workflow_id
        worker.submit(item, revision="recompute-v2")
        assert len(execution.client.list_workflows(application_name=execution.namespace)) == 3
    assert not probe.starts and not probe.closed


def test_equal_cancelled_identity_can_be_inspected_before_public_resume(batch_project):
    root, application, _, _, _ = batch_project
    with Application.open(application, project_root=root, file_logging=False) as app:
        with app.durable(root=root / "persistent") as execution:
            worker = execution.worker("workflows/worker.yaml")
            item = WorkItem("cancelled", {"value": 1})
            handle = worker.submit(item)
            execution.cancel(handle.workflow_id)
            assert execution.status(handle.workflow_id).status == "CANCELLED"
            repeated = worker.submit(item)
            assert repeated.workflow_id == handle.workflow_id
            assert execution.status(handle.workflow_id).status == "CANCELLED"
            resumed = worker.submit(item, resume_terminal=True)
            assert resumed.workflow_id == handle.workflow_id
            assert execution.status(handle.workflow_id).status == "ENQUEUED"


def test_durable_policy_conflicts_and_root_isolation(batch_project):
    root, application, _, _, write = batch_project
    write(application / "config/system.yaml", {"worker_execution": {"workers": {"workflows/worker.yaml": {"concurrency": 2}}}})
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=root / "one") as one, app.durable(root=root / "two") as two:
        first, second = one.worker("workflows/worker.yaml"), two.worker("workflows/worker.yaml")
        assert first.queue_name != second.queue_name
        assert first.policy.concurrency == second.policy.concurrency == 2
        first.submit(WorkItem("same", {"value": 1}))
        second.submit(WorkItem("same", {"value": 2}))
    write(application / "config/system.yaml", {"worker_execution": {"workers": {"workflows/worker.yaml": {"concurrency": 3}}}})
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=root / "one") as execution:
        with pytest.raises(QueueDeclarationConflict):
            execution.worker("workflows/worker.yaml")


def test_storage_target_change_requires_explicit_migration(batch_project):
    root, application, _, _, _ = batch_project
    namespace = root / "persistent"
    with Application.open(application, project_root=root, file_logging=False) as app:
        with app.durable(root=namespace) as execution:
            execution.worker("workflows/worker.yaml").submit(WorkItem("preserved", {"value": 1}))
        with pytest.raises(ValueError, match="Incompatible durable namespace"):
            app.durable(root=namespace, database_url="sqlite:///" + str(root / "other.sqlite"))
        assert not (root / "other.sqlite").exists()
        with app.durable(root=namespace) as execution:
            assert len(execution.client.list_workflows(application_name=execution.namespace)) == 1


def test_two_cli_producers_reject_conflicting_input_without_partial_batch(batch_project, tmp_path):
    root, application, _, _, _ = batch_project
    namespace_root = root / "persistent"
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace_root):
        pass  # Initialize the official schema before the producer race.
    project = Path(__file__).resolve().parents[2]
    code = '''
import json,sys,time
from pathlib import Path
import pytest
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.durable import TaskInputConflict
from test_support.worker_batch import batch_project
case=Path(sys.argv[1]);case.mkdir()
namespace=Path(sys.argv[2]);number=int(sys.argv[3]);gate=Path(sys.argv[4])
patch=pytest.MonkeyPatch()
fixture=batch_project.__wrapped__(case,patch)
root,application,*_=next(fixture)
try:
    with Application.open(application,project_root=root,file_logging=False) as app,app.durable(root=namespace) as execution:
        worker=execution.worker("workflows/worker.yaml")
        read=execution.client.list_workflows
        first=True
        def list_workflows(**kwargs):
            global first
            rows=read(**kwargs)
            if first:
                first=False
                assert not rows
                (gate.parent/f"ready-{number}").touch()
                deadline=time.monotonic()+10
                while not gate.exists():
                    if time.monotonic()>deadline:raise TimeoutError("producer barrier")
                    time.sleep(.01)
            return rows
        execution.client.list_workflows=list_workflows
        try:
            worker.submit_many([WorkItem("shared",{"value":number}),WorkItem(f"unique-{number}",{"value":number})])
        except TaskInputConflict:
            result="input-conflict"
        else:
            result="committed"
        (case/"result.json").write_text(json.dumps({"producer":number,"result":result}))
finally:
    try:next(fixture)
    except StopIteration:pass
    patch.undo()
'''
    gate = tmp_path / "release"
    processes = []
    try:
        for number in (1, 2):
            processes.append(subprocess.Popen([sys.executable, "-c", code, str(tmp_path / f"producer-{number}"),
                str(namespace_root), str(number), str(gate)], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"ready-{n}").exists() for n in (1, 2)):
            if time.monotonic() > deadline:
                raise TimeoutError("producers did not reach public read barrier")
            if any(process.poll() is not None for process in processes):
                raise AssertionError([process.communicate(timeout=1) for process in processes if process.poll() is not None])
            time.sleep(0.02)
        gate.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stdout + stderr
    finally:
        gate.touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    results = [json.loads((tmp_path / f"producer-{n}/result.json").read_text()) for n in (1, 2)]
    assert sorted(result["result"] for result in results) == ["committed", "input-conflict"]
    winner = next(result["producer"] for result in results if result["result"] == "committed")
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace_root) as execution:
        rows = execution.client.list_workflows(application_name=execution.namespace, load_input=True)
        assert len(rows) == 2
        assert {row.input["args"][0]["item"]["id"] for row in rows} == {"shared", f"unique-{winner}"}
