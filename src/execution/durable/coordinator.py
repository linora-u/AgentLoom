"""Business invocation coordination around the shared process-only facade.

DBOS owns queue admission/capacity/retries. These descriptors associate one
accepted attempt with its business and physical receipts; they are not tasks,
claims, leases or an additional capacity ledger.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from filelock import FileLock, Timeout

from .client import TaskDescription, canonical_json
from .diagnostics import error_detail as _error_detail
from .files import atomic_json, read_json
from .host import HostSpec
from .os_job import AdmissionStopped, InvocationFailed, ResourceOwnershipError
from .resource_owner import (
    NotStartedResource,
    exit_receipt_persisted,
    launch_resource,
    preflight_resources,
    resource_observations,
    restore_resource,
)


@dataclass
class _Attempt:
    task: TaskDescription
    job: Path
    cancel: Event = field(default_factory=Event)
    permission: Lock = field(default_factory=Lock)
    resource: object | None = None


class AttemptCoordinator:
    def __init__(self, root: Path, host: HostSpec):
        self._root, self._host_spec, self._host = root, host, host.build()
        self._stop = Event()
        self._external_stop = None
        self._task_state = None
        self._storage_metadata = None
        self._accepted: dict[str, _Attempt] = {}
        self._admission = Lock()

    def belongs_to(self, root, host):
        return self._root == root and self._host_spec == host

    def bind_stop(self, stop):
        self._external_stop = stop

    def bind_task_state(self, read_status):
        self._task_state = read_status

    def bind_storage_metadata(self, metadata):
        self._storage_metadata = metadata

    def preflight(self):
        preflight_resources()

    def active_workflows(self):
        with self._admission:
            return tuple(self._accepted)

    def admission_stopped(self):
        return self._stop.is_set() or (self._external_stop is not None and self._external_stop.is_set())

    def stop_admission(self):
        # Abort before a lock/await, including held business authorization.
        self._stop.set()
        with self._admission:
            accepted = tuple(self._accepted.values())
        for attempt in accepted:
            attempt.cancel.set()
            try:
                atomic_json(attempt.job / "cancelled.json", {"invocation_id": attempt.job.name})
            except OSError as error:
                self._record_error(attempt.job, error, "close_admission")

    def _assert_current(self, attempt):
        if self.admission_stopped() or attempt.cancel.is_set() or (attempt.job / "cancelled.json").exists():
            raise AdmissionStopped("Execution attempt no longer has startup permission")

    def _cancel(self, attempt):
        attempt.cancel.set()
        try:
            atomic_json(attempt.job / "cancelled.json", {"invocation_id": attempt.job.name})
        finally:
            if attempt.resource is not None:
                attempt.resource.request_stop()

    def _authorize(self, attempt):
        with attempt.permission:
            self._assert_current(attempt)
            authorize = getattr(self._host, "authorize", None)
            if authorize is not None:
                authorize(attempt.task, attempt.job.name, lambda: self._assert_current(attempt))
            self._assert_current(attempt)
            atomic_json(attempt.job / "authorized.json", {"invocation_id": attempt.job.name})
            self._assert_current(attempt)
            attempt.resource.release()

    def _revoke(self, attempt):
        with attempt.permission:
            # Revoke idempotently even if a previous diagnostic claims it ran.
            self._host.revoke(attempt.task, attempt.job.name)
            atomic_json(attempt.job / "revoked.json", {"business_authorization_revoked": True})

    def _record_error(self, job, error, phase):
        try:
            path = job / "cleanup-failed.json"
            detail = _error_detail(error, phase, database=self._storage_metadata)
            if path.exists():
                detail = {**read_json(path), "latest_error": detail}
            atomic_json(path, detail)
        except OSError:
            pass  # Diagnostic failure must not replace the original cause.

    def recover(self, *, observation_timeout=1.0):
        """Recover accepted scopes before starting a new DBOS consumer.

        Control callers receive a finite observation/error. They do not declare
        a physical exit when this observation period expires.
        """
        for descriptor in sorted((self._root / "jobs").glob("*/*/description.json")):
            job, value = descriptor.parent, read_json(descriptor)
            if canonical_json(value["host"]) != canonical_json(self._host_spec.to_dict()):
                raise ResourceOwnershipError("Old execution attempt requires its original trusted business host")
            resource = restore_resource(job)
            attempt = _Attempt(TaskDescription.from_dict(value["task"]), job, resource=resource)
            try:
                self._cancel(attempt)
                # Legacy/no-op revokes must not hide a later actual grant.
                self._revoke(attempt)
                resource.wait_exited(timeout=observation_timeout)
                atomic_json(job / "exited.json", {"physical_exit_confirmed": True})
                self._revoke(attempt)
            except Exception as error:
                self._record_error(job, error, "recovery")
                raise ResourceOwnershipError("Resource cleanup remains unconfirmed; inspect resource_status before retrying") from error

    def inspect(self, workflow_id=None):
        return resource_observations(self._root, workflow_id)

    def validate_resume(self, workflow_id):
        validate_resume_resources(self._root, workflow_id)

    @staticmethod
    def _result(job):
        value = read_json(job / "result.json")
        if "error" in value:
            raise InvocationFailed(value["error"], outcome=value.get("outcome"), outcomes=value.get("outcomes"))
        result = value["result"]
        if result.get("status") == "failed":
            raise InvocationFailed(result.get("error") or {"message": "Worker failed"}, outcome=result)
        return result

    async def _await_exit(self, resource):
        # A native scope already has its own notification/cleanup owner. Do
        # not occupy DBOS's shared executor thread for the model's lifetime.
        # Control observations remain bounded; full drain keeps ownership.
        while True:
            observed = resource.observe()
            if observed["physical_exit_confirmed"] or observed.get("not_started"):
                await asyncio.to_thread(resource.wait_exited, timeout=1)
                return
            if observed["status"] == "cleanup_unconfirmed":
                detail = observed.get("error") or {}
                error = ResourceOwnershipError(detail.get("message", "Native cleanup remains unconfirmed"))
                for name in ("errno", "sqlite_errorcode", "sqlite_errorname"):
                    if name in detail:
                        setattr(error, name, detail[name])
                raise error
            await asyncio.sleep(0.05)

    async def _drain(self, attempt, startup, authorization):
        original = None
        if startup is not None:
            try:
                attempt.resource = await asyncio.shield(startup)
            except Exception as error:
                original = error
                if getattr(error, "not_started", False) is True:
                    attempt.resource = NotStartedResource(attempt.job)
        if attempt.resource is not None:
            try:
                await asyncio.to_thread(self._cancel, attempt)
            except Exception as error:
                self._record_error(attempt.job, error, "cancel")
        if authorization is not None:
            try:
                await asyncio.shield(authorization)
            except Exception:
                pass  # Its original error belongs to invoke's result.
        while True:
            try:
                await asyncio.to_thread(self._cancel, attempt)
                await asyncio.to_thread(self._revoke, attempt)
                if attempt.resource is not None:
                    await self._await_exit(attempt.resource)
                elif original is not None:
                    # Only a launcher-certified not-started observation can
                    # permit release. An unknown launch failure retains scope.
                    resource = restore_resource(attempt.job)
                    await self._await_exit(resource)
                atomic_json(attempt.job / "exited.json", {"physical_exit_confirmed": True})
                await asyncio.to_thread(self._revoke, attempt)
                return
            except Exception as error:
                self._record_error(attempt.job, error, "drain")
                # Continued lifecycle reconciliation, not task retry/admission.
                # Public observations retain the original error meanwhile.
                await asyncio.sleep(0.1)

    async def invoke(self, task: TaskDescription):
        directory = self._root / "jobs" / task.workflow_id
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(directory / "scope.lock", thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise ResourceOwnershipError("This task's physical scope has not drained") from error
        attempt, startup, authorization = None, None, None
        try:
            self.validate_prior(task.workflow_id)
            reconciled = await asyncio.to_thread(self._host.reconcile, task)
            if reconciled is not None:
                return reconciled
            previous = sorted(directory.glob("*/description.json"))
            if previous:
                prior = previous[-1].parent
                if (prior / "result.json").exists():
                    try:
                        return self._result(prior)
                    except InvocationFailed:
                        if not self._host_spec.allow_replay:
                            raise
                elif not self._host_spec.allow_replay:
                    raise ResourceOwnershipError("Prior invocation has no result; trusted replay permission is required")
            # Registration and producer cancellation share one short fence.
            # DBOS remains the source of task cancellation; only accepted
            # resource attempts receive a local cancellation observation.
            with FileLock(directory / "registration.lock"), self._admission:
                if self.admission_stopped():
                    raise AdmissionStopped("Executor stopped admission")
                if self._task_state is not None:
                    row = self._task_state(task.workflow_id)
                    if row is None or row.status == "CANCELLED":
                        raise AdmissionStopped("Task was cancelled before attempt registration")
                invocation_id = f"{time.time_ns():020d}-{uuid4().hex}"
                job = directory / invocation_id
                job.mkdir()
                atomic_json(job / "description.json", {"owner": "python", "resource_schema": 1,
                    "invocation_id": invocation_id, "task": task.to_dict(), "host": self._host_spec.to_dict()})
                attempt = _Attempt(task, job)
                self._accepted[task.workflow_id] = attempt
            startup = asyncio.create_task(asyncio.to_thread(launch_resource, job))
            attempt.resource = await asyncio.shield(startup)
            authorization = asyncio.create_task(asyncio.to_thread(self._authorize, attempt))
            await asyncio.shield(authorization)
            await self._await_exit(attempt.resource)
            atomic_json(job / "exited.json", {"physical_exit_confirmed": True})
            if not (job / "result.json").exists():
                raise InvocationFailed({"message": "Application exited without a result", "category": "process_exit"})
            return self._result(job)
        except Exception as error:
            if attempt is not None and not isinstance(error, AdmissionStopped):
                try:
                    atomic_json(attempt.job / "invocation-failed.json",
                                _error_detail(error, "invoke", database=self._storage_metadata))
                except OSError:
                    pass  # Preserve the original invocation exception.
            raise
        finally:
            if attempt is not None:
                attempt.cancel.set()
                # Close child dispatch before joining an authorizer whose
                # release handshake may still be in flight.
                try:
                    atomic_json(attempt.job / "cancelled.json", {"invocation_id": attempt.job.name})
                except OSError as error:
                    self._record_error(attempt.job, error, "cancel")
                cleanup = asyncio.create_task(self._drain(attempt, startup, authorization))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                with self._admission:
                    self._accepted.pop(task.workflow_id, None)
            lock.release()

    def validate_prior(self, workflow_id):
        for item in self.inspect(workflow_id):
            if not item["physical_exit_confirmed"]:
                raise ResourceOwnershipError("Prior invocation has not physically exited")
        for descriptor in (self._root / "jobs" / workflow_id).glob("*/description.json"):
            if not exit_receipt_persisted(descriptor.parent):
                raise ResourceOwnershipError("Prior invocation exit receipt is not persisted")


class LegacyCoordinator:
    """Compatibility only for explicitly injected historical jobs objects."""
    def __init__(self, jobs):
        self.jobs = jobs

    def belongs_to(self, root, host):
        return self.jobs.root == root and self.jobs.host_spec == host

    def bind_stop(self, stop):
        self.jobs.stop_token = stop

    def bind_task_state(self, read_status):
        pass

    def bind_storage_metadata(self, metadata):
        pass

    def active_workflows(self):
        return tuple(self.jobs.active)

    def preflight(self):
        pass

    def __getattr__(self, name):
        return getattr(self.jobs, name)


def validate_resume_resources(root, workflow_id):
    directory = root / "jobs" / workflow_id
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / "scope.lock", thread_local=False)
    try:
        lock.acquire(timeout=0)
    except Timeout as error:
        raise ResourceOwnershipError("Old execution attempt has not drained") from error
    try:
        if any(not item["physical_exit_confirmed"] for item in resource_observations(root, workflow_id)):
            raise ResourceOwnershipError("Old execution attempt has not physically exited")
        for descriptor in directory.glob("*/description.json"):
            if not exit_receipt_persisted(descriptor.parent):
                raise ResourceOwnershipError("Required execution exit receipt is not persisted")
    finally:
        lock.release()
