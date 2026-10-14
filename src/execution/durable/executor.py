"""A dedicated local DBOS consumer. No framework task claims or lease loop."""
from __future__ import annotations

import os
import time
from threading import Event

import psutil
from filelock import FileLock, Timeout

from .client import WORKFLOW_NAME, WORKFLOW_VERSION, TaskDescription
from .diagnostics import attach_persistence_trace, detach_persistence_trace, storage_metadata
from .files import atomic_json, read_json
from .host import HostSpec
from .os_job import AdmissionStopped, InvocationFailed, OwnedJobs, ResourceOwnershipError, process_alive


class DurableExecutor:
    """One consumer process, independent producer CLIs, per-Worker DBOS queues.

    A consumer process must exit after ``run``. The concrete executor identity
    prevents replacement while an old process is still alive, even after a
    released file lock or DBOS.destroy(). The host's retry decision is explicit;
    Runtime error metadata alone never authorizes generic replay.
    """
    def __init__(self, execution, workers, *, host: HostSpec | None = None, jobs: OwnedJobs | None = None):
        execution._require_open()
        self.execution = execution
        self.workers = {worker.worker.identity: worker for worker in (execution.worker(path) for path in workers)}
        if not self.workers:
            raise ValueError("Executor requires trusted Worker registrations")
        app = execution.application
        self.host = host or HostSpec("agentloom.execution.durable.host:application_host",
            {"application": str(app.application_root), "project_root": str(app.project_root)})
        from agentloom.app.composition import build_durable_attempt_coordinator
        self.jobs = build_durable_attempt_coordinator(execution.root, self.host, jobs=jobs)
        if not self.jobs.belongs_to(execution.root, self.host):
            raise ValueError("Process owner must belong to this execution root and trusted host")
        self._used = False

    def run(self, *, stop_token: Event | None = None, idle_timeout: float | None = None):
        """Serve until explicitly stopped, or until an optional idle interval.

        Stop refuses new OS starts, requests upstream cancellation and waits
        for concrete Application scopes. It does not report DBOS status or
        destroy() as physical drain. Unprocessed queue items stay durable.
        """
        from dbos import DBOS, DBOSPortableJSONSerializer, WorkflowSerializationFormat
        from dbos import error as dbos_error

        if self._used:
            raise RuntimeError("A durable executor requires a fresh dedicated process")
        self._used = True
        stop = stop_token or Event()
        self.jobs.bind_stop(stop)
        self.jobs.bind_task_state(self.execution.status)
        self.jobs.bind_storage_metadata(storage_metadata(self.execution.root, self.execution.database_url))
        root = self.execution.root
        lock = FileLock(root / "executor.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise ResourceOwnershipError("Another local durable executor owns this namespace") from error
        launched = False
        trace = None
        try:
            marker = root / "executor.json"
            if marker.exists() and process_alive(read_json(marker)):
                raise ResourceOwnershipError("Prior durable executor process has not exited")
            for worker in self.workers.values():
                worker.verify_queue()
            self.jobs.preflight()
            self.jobs.recover()
            if stop.is_set():
                return
            atomic_json(marker, {"pid": os.getpid(), "creation": psutil.Process().create_time()})

            @DBOS.workflow(name=WORKFLOW_NAME, serialization_type=WorkflowSerializationFormat.PORTABLE)
            async def execute(payload):
                task = TaskDescription.from_dict(payload)
                if task.namespace != self.execution.namespace or task.worker not in self.workers or task.workflow_id != DBOS.workflow_id:
                    raise ValueError("Task is outside the trusted executor registration")
                async def invoke_owned(task):
                    try:
                        return await self.jobs.invoke(task)
                    except AdmissionStopped as error:
                        await self.execution.client.cancel_workflow_async(task.workflow_id)
                        raise dbos_error.DBOSWorkflowCancelledError(task.workflow_id) from error

                return await DBOS.run_step_async({"name": "os_owned_application_v1", "preemptible": True,
                    "retries_allowed": self.host.allow_replay, "max_attempts": self.host.max_attempts,
                    "should_retry": lambda error: isinstance(error, InvocationFailed) and error.retryable},
                    invoke_owned, task)

            DBOS(config={"name": self.execution.namespace, "application_version": WORKFLOW_VERSION,
                "system_database_url": self.execution.database_url, "serializer": DBOSPortableJSONSerializer(),
                "enable_otlp": False, "run_migrations": False,
                "max_executor_threads": sum(worker.policy.concurrency for worker in self.workers.values())})
            trace = attach_persistence_trace(root, self.execution.database_url)
            DBOS.listen_queues([worker.queue_name for worker in self.workers.values()])
            DBOS.launch()
            launched = True
            atomic_json(root / "ready.json", {"pid": os.getpid(), "creation": psutil.Process().create_time(),
                                              "namespace": self.execution.namespace})
            idle_since = None
            while not stop.wait(0.1):
                if idle_timeout is None:
                    continue
                rows = self.execution.client.list_workflows(application_name=self.execution.namespace,
                    status=["ENQUEUED", "PENDING"], load_input=False, load_output=False)
                idle = not rows and not self.jobs.active_workflows()
                idle_since = (idle_since or time.monotonic()) if idle else None
                if idle_since is not None and time.monotonic() - idle_since >= idle_timeout:
                    break
        finally:
            self.jobs.stop_admission()
            if launched:
                for workflow_id in self.jobs.active_workflows():
                    self.execution.client.cancel_workflow(workflow_id)
                # The public preemptible async step awaits its cleanup. Status
                # may already be CANCELLED while this concrete scope is alive.
                while self.jobs.active_workflows():
                    time.sleep(0.05)
                DBOS.destroy(destroy_registry=True, workflow_completion_timeout_sec=30)
            if trace is not None:
                detach_persistence_trace(trace)
            lock.release()
