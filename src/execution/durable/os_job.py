"""A process owner runs each entire Application; DBOS owns admission and retry.

Resource descriptors retain actual OS identities/results across executor loss.
They are not pending tasks or leases. No successor invocation can use this
workflow's OS scope until its concrete root and group have disappeared.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from threading import Lock
from uuid import uuid4

from filelock import FileLock, Timeout
import psutil

from .client import TaskDescription, canonical_json
from .files import atomic_json, read_json
from .host import HostSpec


class ResourceOwnershipError(RuntimeError):
    pass


class AdmissionStopped(ResourceOwnershipError):
    pass


class InvocationFailed(RuntimeError):
    def __init__(self, detail, *, outcome=None, outcomes=None):
        if not isinstance(detail, dict):
            detail = {"message": str(detail)}
        self.detail = detail
        # Portable DBOS errors preserve public `data`, not arbitrary exception
        # attributes. Keep the complete Worker failure/usage receipt here.
        self.data = {"error": detail, "outcome": outcome, "outcomes": outcomes or []}
        self.retryable = (bool(detail["retryable"]) if detail.get("retryable") is not None
                          else detail.get("category") == "output_validation")
        super().__init__(detail.get("message", "Application invocation failed"))


def process_alive(identity: dict) -> bool:
    try:
        process = psutil.Process(identity["pid"])
        return process.create_time() == identity["creation"] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def group_alive(identity: dict) -> bool:
    try:
        os.killpg(identity["group"], 0)
    except ProcessLookupError:
        return False
    return True


def job_exited(job: Path) -> bool:
    if (job / "exited.json").exists():
        # Ownership ended when physical exit was confirmed. A later process
        # using the same numeric PGID does not belong to this old invocation.
        return read_json(job / "exited.json")["physical_exit_confirmed"] is True
    if read_json(job / "description.json").get("owner") == "openclaw":
        # Only the public SDK's completed cleanup can certify this invocation.
        # A root exit or an empty group alone cannot erase a failed SDK join.
        return False
    path = job / "identity.json"
    if not path.exists():
        raise ResourceOwnershipError("OS job identity is unknown")
    identity = read_json(path)
    return not process_alive(identity) and not group_alive(identity)


class OwnedJobs:
    """Shared invocation/receipt contract; native lifetime belongs to an Adapter."""
    owner: str

    def __init__(self, root: Path, host: HostSpec):
        self.root, self.host_spec, self.host = root, host, host.build()
        self.active: dict[str, Path] = {}
        self.stopping = False
        self.stop_token = None
        self.admission = Lock()

    def admission_stopped(self):
        return self.stopping or (self.stop_token is not None and self.stop_token.is_set())

    def stop_admission(self):
        with self.admission:
            self.stopping = True

    def _start(self, job: Path):
        raise NotImplementedError

    def _terminate(self, job: Path):
        raise NotImplementedError

    def _stop_sync(self, job: Path, *, revoke=True):
        descriptor = read_json(job / "description.json")
        error = None
        if revoke and not (job / "revoked.json").exists():
            try:
                self.host.revoke(TaskDescription.from_dict(descriptor["task"]), descriptor["invocation_id"])
                atomic_json(job / "revoked.json", {"business_authorization_revoked": True})
            except Exception as caught:
                error = caught
        if (job / "exited.json").exists():
            self._terminate(job)
            if error is not None:
                raise error
            return
        self._terminate(job)
        atomic_json(job / "exited.json", {"physical_exit_confirmed": True, "time_ns": time.time_ns()})
        if error is not None:
            raise error

    def recover(self):
        """Called with the local executor lock held, before DBOS.launch()."""
        for descriptor in sorted((self.root / "jobs").glob("*/*/description.json")):
            job = descriptor.parent
            value = read_json(descriptor)
            if (job / "exited.json").exists():
                self._terminate(job)
                if not (job / "revoked.json").exists():
                    if canonical_json(value["host"]) != canonical_json(self.host_spec.to_dict()):
                        raise ResourceOwnershipError("Old business grant uses another host; recover with its original host")
                    self.host.revoke(TaskDescription.from_dict(value["task"]), value["invocation_id"])
                    atomic_json(job / "revoked.json", {"business_authorization_revoked": True})
                continue
            if canonical_json(value["host"]) != canonical_json(self.host_spec.to_dict()):
                raise ResourceOwnershipError("Old OS job uses another trusted host; stop it with its original host")
            self._stop_sync(job, revoke=not (job / "exited.json").exists())

    def _result(self, job):
        value = read_json(job / "result.json")
        if "error" in value:
            raise InvocationFailed(value["error"], outcome=value.get("outcome"), outcomes=value.get("outcomes"))
        result = value["result"]
        if result.get("status") == "failed":
            raise InvocationFailed(result.get("error") or {"message": "Worker failed"}, outcome=result)
        return result

    async def invoke(self, task: TaskDescription):
        directory = self.root / "jobs" / task.workflow_id
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(directory / "scope.lock", thread_local=False)
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise ResourceOwnershipError("This task's OS invocation has not drained") from error
        job = None
        try:
            if self.admission_stopped():
                raise AdmissionStopped("Executor stopped admission")
            previous = sorted(directory.glob("*/description.json"))
            if previous:
                prior = previous[-1].parent
                if not (prior / "exited.json").exists() or ((prior / "identity.json").exists() and not job_exited(prior)):
                    raise ResourceOwnershipError("Prior OS invocation has not physically exited")
            # A business receipt proves commit, never OS scope drain.
            reconciled = await asyncio.to_thread(self.host.reconcile, task)
            if reconciled is not None:
                return reconciled
            if previous:
                if (prior / "result.json").exists():
                    try:
                        return self._result(prior)
                    except InvocationFailed:
                        if not self.host_spec.allow_replay:
                            raise
                elif not self.host_spec.allow_replay:
                    raise ResourceOwnershipError("Prior invocation has no result; host replay authorization is required")
            with self.admission:
                if self.admission_stopped():
                    raise AdmissionStopped("Executor stopped admission")
                invocation_id = f"{time.time_ns():020d}-{uuid4().hex}"
                job = directory / invocation_id
                job.mkdir()
                label = "dev.agentloom.job." + uuid4().hex
                atomic_json(job / "description.json", {"owner": self.owner, "label": label, "invocation_id": invocation_id,
                    "task": task.to_dict(), "host": self.host_spec.to_dict()})
                self.active[task.workflow_id] = job
                # Finish native admission before cancellation can run cleanup.
                self._start(job)
            deadline = time.monotonic() + 30
            while not (job / "identity.json").exists():
                if time.monotonic() > deadline:
                    raise ResourceOwnershipError("OS job did not complete identity handshake; see its stderr.log")
                await asyncio.sleep(0.02)
            identity = read_json(job / "identity.json")
            if identity["group"] != identity["pid"] or not process_alive(identity):
                raise ResourceOwnershipError("OS job identity handshake is invalid")
            with self.admission:
                if self.admission_stopped():
                    raise AdmissionStopped("Executor stopped before business authorization")
                atomic_json(job / "authorized.json", {"invocation_id": invocation_id})
            while not job_exited(job):
                if (job / "openclaw-cleanup-failed.json").exists():
                    raise ResourceOwnershipError("OpenClaw cleanup failed; capacity remains retained")
                await asyncio.sleep(0.05)
            atomic_json(job / "exited.json", {"physical_exit_confirmed": True, "time_ns": time.time_ns()})
            if not (job / "result.json").exists():
                raise InvocationFailed({"message": "Application exited without a result", "category": "process_exit"})
            return self._result(job)
        finally:
            try:
                if job is not None:
                    # Upstream preemptible step awaits cancellation cleanup.
                    # Shield concrete drain against further host cancellation.
                    cleanup = asyncio.create_task(asyncio.to_thread(self._stop_sync, job))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                        except BaseException:
                            # Await propagates the cleanup Task's own error.
                            # Inspect it below before releasing OS ownership.
                            break
                    try:
                        cleanup.result()
                    except BaseException as error:
                        try:
                            atomic_json(job / "cleanup-failed.json", {"error_type": type(error).__name__, "message": str(error)})
                        except OSError:
                            pass  # diagnostic persistence failure never releases a live scope
                        # A failed OS control operation cannot release capacity.
                        # Preserve its cause while waiting for externally
                        # confirmed physical exit; no claim/lease retry here.
                        while True:
                            try:
                                if job_exited(job):
                                    break
                            except Exception:
                                pass
                            try:
                                await asyncio.sleep(0.1)
                            except asyncio.CancelledError:
                                continue
                        raise
            finally:
                self.active.pop(task.workflow_id, None)
                lock.release()


class LaunchdJobs(OwnedJobs):
    """Legacy Adapter, retained to drain previously loaded macOS jobs."""
    owner = "launchd"

    def __init__(self, root: Path, host: HostSpec):
        if sys.platform != "darwin":
            raise RuntimeError("The legacy launchd process owner is only available on macOS")
        super().__init__(root, host)
        self.domain = f"gui/{os.getuid()}"

    def _command(self, *arguments):
        return subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True)

    def _start(self, job: Path):
        label = read_json(job / "description.json")["label"]
        plist = job / "job.plist"
        plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": [sys.executable,
            "-m", "agentloom.execution.durable.job_entry", str(job)], "RunAtLoad": True,
            "KeepAlive": False, "AbandonProcessGroup": False, "ExitTimeOut": 5,
            "WorkingDirectory": str(Path.cwd()),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": os.pathsep.join(str(Path(p or ".").resolve()) for p in sys.path)},
            "StandardOutPath": str(job / "stdout.log"), "StandardErrorPath": str(job / "stderr.log")}))
        boot = self._command("bootstrap", self.domain, str(plist))
        if boot.returncode:
            raise ResourceOwnershipError("launchd bootstrap failed: " + boot.stderr.strip())

    def _remove(self, job: Path):
        if (job / "unloaded.json").exists():
            return
        result = self._command("bootout", self.domain + "/" + read_json(job / "description.json")["label"])
        if result.returncode != 0:
            raise ResourceOwnershipError("launchd bootout failed: " + result.stderr.strip())
        atomic_json(job / "unloaded.json", {"control_operation_completed": True})

    def _discover_identity(self, job: Path):
        path = job / "identity.json"
        if path.exists():
            return read_json(path)
        for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
            argv = process.info["cmdline"] or []
            if argv[-3:] == ["-m", "agentloom.execution.durable.job_entry", str(job)]:
                try:
                    group = os.getpgid(process.pid)
                except ProcessLookupError:
                    continue
                identity = {"pid": process.pid, "creation": process.info["create_time"], "group": group}
                if identity["pid"] != identity["group"]:
                    raise ResourceOwnershipError("Recovered OS job no longer owns its group")
                atomic_json(path, identity)
                return identity
        if (job / "authorized.json").exists():
            raise ResourceOwnershipError("Authorized OS job lost its identity; refusing replacement")
        return None

    def _terminate(self, job: Path):
        if read_json(job / "description.json").get("owner", "launchd") != self.owner:
            raise ResourceOwnershipError("Use the original process owner to drain this invocation")
        self._remove(job)
        if (job / "exited.json").exists():
            return
        identity = self._discover_identity(job)
        if identity is not None:
            while group_alive(identity) or process_alive(identity):
                time.sleep(0.02)
