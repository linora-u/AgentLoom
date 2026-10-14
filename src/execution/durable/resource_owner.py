"""Process-only adapter; task replay and business permissions live elsewhere."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .files import atomic_json, read_json
from .os_job import LaunchdJobs, ResourceOwnershipError, process_alive


def preflight_resources():
    if sys.platform not in {"darwin", "linux"}:
        raise ResourceOwnershipError("Managed durable execution requires macOS or Linux")
    # Import/capability errors must occur before DBOS consumes any task.
    from agentloom.execution.managed_process import (
        launch_owned_process,
        recover_owned_process,
        validate_process_support,
    )
    if not callable(launch_owned_process) or not callable(recover_owned_process):
        raise ResourceOwnershipError("Managed process implementation is unavailable")
    validate_process_support()


def launch_resource(job: Path):
    from agentloom.execution.managed_process import launch_owned_process

    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(Path(path or ".").resolve()) for path in sys.path)
    with (job / "stdout.log").open("wb") as stdout, (job / "stderr.log").open("wb") as stderr:
        return launch_owned_process(
            [sys.executable, "-m", "agentloom.execution.durable.job_entry", str(job)],
            env=environment, cwd=str(Path.cwd()), stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, receipt_dir=job / "resource", gated=True,
        )


class HistoricalResource:
    """Read old physical receipts without loading a removed upstream package."""

    def __init__(self, job, descriptor):
        self.job, self.descriptor = job, descriptor

    def observe(self):
        receipt = self.job / "exited.json"
        confirmed = receipt.exists() and read_json(receipt).get("physical_exit_confirmed") is True
        guardian = self.job / "guardian.json"
        if confirmed and guardian.exists() and process_alive(read_json(guardian)):
            confirmed = False
        return {"status": "exited" if confirmed else "cleanup_unconfirmed",
                "physical_exit_confirmed": confirmed,
                "error": None if confirmed else {"error_type": "ResourceOwnershipError",
                    "message": "Original resource owner has not confirmed exit; automatic migration is refused"}}

    def request_stop(self):
        if self.observe()["physical_exit_confirmed"]:
            return
        if self.descriptor.get("owner", "launchd") == "launchd":
            from .host import HostSpec

            # The control ACK closes legacy admission; it does not prove drain.
            LaunchdJobs(self.job.parents[2], HostSpec(**self.descriptor["host"]))._remove(self.job)
            return
        raise ResourceOwnershipError("Old resource lacks confirmed exit; its original owner must drain it")

    def wait_exited(self, timeout=None):
        if not self.observe()["physical_exit_confirmed"] and self.descriptor.get("owner", "launchd") == "launchd":
            from .host import HostSpec

            LaunchdJobs(self.job.parents[2], HostSpec(**self.descriptor["host"]))._terminate(
                self.job, observation_timeout=timeout)
            atomic_json(self.job / "exited.json", {"physical_exit_confirmed": True})
        if not self.observe()["physical_exit_confirmed"]:
            raise ResourceOwnershipError("Old resource exit remains unconfirmed")
        return self.observe()


class NotStartedResource:
    """A launcher's explicit proof that no transport/native scope was created."""

    def __init__(self, job):
        self.job = job

    def observe(self):
        path = self.job / "resource-not-started.json"
        confirmed = path.exists() and read_json(path) == {"invocation_id": self.job.name, "not_started": True}
        return {"status": "exited" if confirmed else "cleanup_unconfirmed",
                "physical_exit_confirmed": confirmed, "not_started": True, "error": None}

    def request_stop(self):
        pass

    def wait_exited(self, timeout=None):
        atomic_json(self.job / "resource-not-started.json", {"invocation_id": self.job.name, "not_started": True})
        return self.observe()


def restore_resource(job: Path):
    descriptor = read_json(job / "description.json")
    if descriptor.get("owner") != "python":
        if descriptor.get("owner", "launchd") not in {"launchd", "openclaw"}:
            raise ResourceOwnershipError("Unknown original resource owner; migration is refused")
        return HistoricalResource(job, descriptor)
    if descriptor.get("resource_schema") != 1:
        raise ResourceOwnershipError("Unsupported execution resource description")
    if (job / "resource-not-started.json").exists():
        if read_json(job / "resource-not-started.json") != {"invocation_id": job.name, "not_started": True}:
            raise ResourceOwnershipError("Unrecognized not-started proof; replacement is refused")
        return NotStartedResource(job)
    from agentloom.execution.managed_process import recover_owned_process
    return recover_owned_process(job / "resource")


def exit_receipt_persisted(job: Path):
    try:
        return read_json(job / "exited.json").get("physical_exit_confirmed") is True
    except (OSError, ValueError, AttributeError):
        return False


def resource_observations(root: Path, workflow_id: str | None = None):
    from .client import TaskDescription
    directory = root / "jobs" / workflow_id if workflow_id else root / "jobs"
    pattern = "*/description.json" if workflow_id else "*/*/description.json"
    observations = []
    for descriptor in sorted(directory.glob(pattern)):
        value = read_json(descriptor)
        try:
            observation = restore_resource(descriptor.parent).observe()
        except Exception as error:
            observation = {"status": "cleanup_unconfirmed", "physical_exit_confirmed": False,
                           "error": {"error_type": type(error).__name__, "message": str(error)}}
        cleanup = descriptor.parent / "cleanup-failed.json"
        if cleanup.exists():
            observation = {**observation, "cleanup_error": read_json(cleanup)}
        invocation_error = descriptor.parent / "invocation-failed.json"
        if invocation_error.exists():
            observation = {**observation, "invocation_error": read_json(invocation_error)}
        persistence_error = descriptor.parent.parent / "persistence-error.json"
        if persistence_error.exists():
            observation = {**observation, "persistence_error": read_json(persistence_error)}
        observations.append({**observation, "exit_receipt_persisted": exit_receipt_persisted(descriptor.parent),
                             "invocation_id": value["invocation_id"],
                             "workflow_id": TaskDescription.from_dict(value["task"]).workflow_id})
    return observations
