"""Typed Application submissions; DBOS owns every persistent task record.

No task table, claim loop, lease or durable slot exists in this adapter. Input
conflicts use the public workflow input. Batch inserts use the public DBOS
transaction API, with the original system database configuration unchanged.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentloom.execution.concurrency import WorkItem, WorkPlan
from agentloom.execution.concurrency.policy import resolve_worker_execution

if TYPE_CHECKING:
    from agentloom.app.application import Application, BoundWorker

WORKFLOW_NAME = "agentloom_worker_v1"
WORKFLOW_VERSION = "agentloom-worker-v1"
TASK_SCHEMA_VERSION = 1


class TaskInputConflict(ValueError):
    """A stable identity already belongs to a different immutable input."""


class QueueDeclarationConflict(ValueError):
    """A persistent capacity declaration differs from this Worker policy."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def identity_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class TaskDescription:
    namespace: str
    worker: str
    item: WorkItem
    revision: str

    def __post_init__(self):
        if not isinstance(self.namespace, str) or not self.namespace.startswith("agentloom-"):
            raise ValueError("invalid durable namespace")
        from agentloom.app.application import _worker_identity
        if not isinstance(self.worker, str) or _worker_identity(self.worker) != self.worker:
            raise ValueError("invalid canonical Worker identity")
        if not isinstance(self.item, WorkItem):
            raise TypeError("durable task requires a WorkItem")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("business revision must be a non-empty string")

    @property
    def workflow_id(self) -> str:
        return self.workflow_id_for(self.namespace, self.worker, self.item.id, self.revision)

    @staticmethod
    def workflow_id_for(namespace: str, worker: str, item_id: str, revision: str) -> str:
        return "loom-" + identity_digest([namespace, worker, item_id, revision])

    def to_dict(self) -> dict:
        return {"schema_version": TASK_SCHEMA_VERSION, "namespace": self.namespace,
                "worker": self.worker, "item": self.item.to_dict(), "revision": self.revision}

    @classmethod
    def from_dict(cls, value: dict) -> TaskDescription:
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", "namespace", "worker", "item", "revision"}
                or type(value["schema_version"]) is not int or value["schema_version"] != TASK_SCHEMA_VERSION
                or not isinstance(value["item"], dict) or set(value["item"]) != {"id", "input"}):
            raise ValueError("Unsupported durable task schema")
        return cls(value["namespace"], value["worker"], WorkItem(**value["item"]), value["revision"])


class DurableApplication:
    """A local namespace by default; another DBOS database URL is explicit.

    Different roots have different queue identities, including with a shared
    remote database. This supplies storage replacement, not multi-host resource
    ownership. The single-machine executor remains a separate trusted host.
    """

    def __init__(self, application: Application, *, root=None, database_url: str | None = None):
        try:
            from dbos import DBOSClient, DBOSPortableJSONSerializer
        except ImportError as error:
            raise RuntimeError("Persistent execution requires AgentLoom's durable extra") from error
        self.application = application
        application_id = application.runtime_context.application_id
        default = application.runtime_context.root_dir / "durable" / application_id
        self.root = Path(root or default).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.namespace = "agentloom-" + identity_digest([str(self.root), application_id])[:32]
        self.database_url = database_url or "sqlite:///" + str(self.root / "system.sqlite")
        from .files import initialize_namespace
        initialize_namespace(self.root, self.database_url)
        self._closed = False
        self._workers: dict[str, DurableWorker] = {}
        self.client = DBOSClient(system_database_url=self.database_url, application_name=self.namespace,
                                 serializer=DBOSPortableJSONSerializer())

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self.client.destroy()

    def _require_open(self):
        if self._closed:
            raise RuntimeError("Durable Application connection is closed")
        self.application._require_open()

    def worker(self, path: str | Path) -> DurableWorker:
        self._require_open()
        worker = self.application.worker(path)
        if worker.identity not in self._workers:
            self._workers[worker.identity] = DurableWorker(self, worker)
        return self._workers[worker.identity]

    def status(self, workflow_id: str):
        self._require_open()
        rows = self.client.list_workflows(workflow_ids=[workflow_id], application_name=self.namespace,
                                          load_input=False, load_output=True)
        return rows[0] if rows else None

    def cancel(self, workflow_id: str):
        """Request cancellation; completion is observed separately from this ACK."""
        if self.status(workflow_id) is None:
            raise ValueError("Task does not belong to this durable namespace")
        self.client.cancel_workflow(workflow_id)

    def executor(self, workers: Sequence[str], *, host=None, jobs=None):
        """Configure a dedicated local consumer from trusted Worker paths."""
        from .executor import DurableExecutor
        return DurableExecutor(self, workers, host=host, jobs=jobs)

    def resume(self, workflow_id: str):
        """Resume a terminal task only after its old OS invocation drains.

        Direct SDK resume bypasses this resource contract. Use this Interface
        and always retain the original stage queue rather than DBOS's default
        internal queue.
        """
        from filelock import FileLock, Timeout
        from .os_job import ResourceOwnershipError, job_exited
        row = self.status(workflow_id)
        if row is None or row.input is None:
            # status normally omits input; fetch the public immutable payload.
            rows = self.client.list_workflows(workflow_ids=[workflow_id], application_name=self.namespace,
                load_input=True, load_output=False)
            if not rows:
                raise ValueError("Unknown durable task")
            row = rows[0]
        task = TaskDescription.from_dict(row.input["args"][0])
        worker = self.worker(task.worker)
        if task.workflow_id != workflow_id or task.namespace != self.namespace:
            raise TaskInputConflict("Durable task identity does not match its payload")
        if row.status == "SUCCESS":
            return self.client.retrieve_workflow(workflow_id)
        if row.status not in {"CANCELLED", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}:
            raise ValueError("Only a terminal durable task may be resumed")
        directory = self.root / "jobs" / workflow_id
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(directory / "scope.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise ResourceOwnershipError("Old OS invocation has not drained") from error
        try:
            for descriptor in directory.glob("*/description.json"):
                job = descriptor.parent
                if not (job / "exited.json").exists() or ((job / "identity.json").exists() and not job_exited(job)):
                    raise ResourceOwnershipError("Old OS invocation has not physically exited")
        finally:
            lock.release()
        if row.status == "CANCELLED":
            return self.client.resume_workflow(workflow_id, queue_name=worker.queue_name)
        # DBOS resume is a no-op for recorded ERROR. Its public terminal-only
        # rewind clears the failed step; host receipt/replay policy still gates
        # whether an actual new Application invocation may occur.
        return self.client.rewind_workflow(workflow_id, start_step=1, queue_name=worker.queue_name)


class DurableWorker:
    """A persisted queue declaration bound to one canonical Worker identity."""

    def __init__(self, execution: DurableApplication, worker: BoundWorker):
        self.execution, self.worker = execution, worker
        owner = execution.application
        self.policy = resolve_worker_execution(worker.identity,
            project=owner.project_config.raw.get("worker_execution"),
            application=owner.application_config.get("worker_execution"))
        self.queue_name = "loom-queue-" + identity_digest([execution.namespace, worker.identity])[:40]
        queue = execution.client.register_queue(self.queue_name, worker_concurrency=self.policy.concurrency,
            application_name=execution.namespace, on_conflict="never_update")
        self.verify_queue(queue)

    def verify_queue(self, queue=None):
        queue = queue or self.execution.client.retrieve_queue(self.queue_name)
        if queue is None or (queue.application_name, queue.worker_concurrency, queue.global_concurrency,
                             queue.partition_concurrency, queue.partition_worker_concurrency, queue.limiter,
                             queue.partition_limiter) != (self.execution.namespace, self.policy.concurrency,
                                                          None, None, None, None, None):
            raise QueueDeclarationConflict(f"Persistent Worker policy conflicts: {self.worker.identity}")

    def description(self, item: WorkItem, revision="1") -> TaskDescription:
        if not isinstance(item, WorkItem):
            raise TypeError("Durable submission requires a WorkItem")
        return TaskDescription(self.execution.namespace, self.worker.identity, item, revision)

    def submit(self, item: WorkItem, *, revision="1"):
        return self.submit_many([item], revision=revision)[0]

    def submit_many(self, items: Sequence[WorkItem] | WorkPlan, *, revision="1"):
        """Atomically submit a finite plan, preserving existing equal tasks.

        A concurrent duplicate rejects the entire attempted insert transaction.
        The next public read validates that winner, then inserts only missing
        tasks. Each collision adds a known existing ID; no task is claimed here.
        """
        from dbos import WorkflowSerializationFormat, error as dbos_error
        from sqlalchemy import create_engine

        self.execution._require_open()
        self.verify_queue()
        plan = items if isinstance(items, WorkPlan) else WorkPlan(items)
        descriptions = [self.description(item, revision) for item in plan.items]
        if not descriptions:
            return []
        expected = {task.workflow_id: task.to_dict() for task in descriptions}
        client = self.execution.client
        engine = create_engine(self.execution.database_url)
        try:
            # With immutable task IDs, every conflict consumes at least one
            # missing ID. A concurrent destructive external API change instead
            # fails visibly rather than spinning or inventing a task identity.
            for _ in range(len(descriptions) + 1):
                existing = client.list_workflows(workflow_ids=list(expected), load_input=True, load_output=False)
                present = set()
                for row in existing:
                    inputs = row.input
                    if (row.application_name != self.execution.namespace or row.queue_name != self.queue_name
                            or row.name != WORKFLOW_NAME or inputs is None or inputs["kwargs"]
                            or len(inputs["args"]) != 1 or canonical_json(inputs["args"][0]) != canonical_json(expected[row.workflow_id])):
                        raise TaskInputConflict("Durable identity already has a different input")
                    present.add(row.workflow_id)
                # Existing immutable tasks use their original contract. A
                # changed current schema must not hide a completed result.
                # Validate every new item before any insertion in this batch.
                if self.worker._input_validator is not None:
                    for task in descriptions:
                        if task.workflow_id not in present:
                            self.worker._input_validator(task.item.mutable_input())
                try:
                    with engine.begin() as connection:
                        for task in descriptions:
                            if task.workflow_id in present:
                                continue
                            client.enqueue_in_transaction(connection, {
                                "workflow_name": WORKFLOW_NAME, "queue_name": self.queue_name,
                                "workflow_id": task.workflow_id, "workflow_id_reuse_policy": "reject",
                                "application_name": self.execution.namespace, "app_version": WORKFLOW_VERSION,
                                "serialization_type": WorkflowSerializationFormat.PORTABLE,
                                "attributes": {"worker_policy": self.policy.to_dict()},
                            }, task.to_dict())
                    return [client.retrieve_workflow(task.workflow_id) for task in descriptions]
                except dbos_error.DBOSWorkflowIDInUseError:
                    continue
            raise RuntimeError("Concurrent durable identity changes prevented atomic submission")
        finally:
            engine.dispose()
