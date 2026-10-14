"""Process-only adapter; task replay and business permissions live elsewhere."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .files import read_json
from .os_job import LaunchdJobs, ResourceOwnershipError, process_alive


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
            LaunchdJobs(self.job.parents[2], HostSpec(**self.descriptor["host"]))._terminate(self.job)
            return
        raise ResourceOwnershipError("Old resource lacks confirmed exit; its original owner must drain it")

    def wait_exited(self, timeout=None):
        if not self.observe()["physical_exit_confirmed"]:
            raise ResourceOwnershipError("Old resource exit remains unconfirmed")
        return self.observe()


def restore_resource(job: Path):
    descriptor = read_json(job / "description.json")
    if descriptor.get("owner") != "python":
        return HistoricalResource(job, descriptor)
    if descriptor.get("resource_schema") != 1:
        raise ResourceOwnershipError("Unsupported execution resource description")
    from agentloom.execution.managed_process import recover_owned_process
    return recover_owned_process(job / "resource")


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
        observations.append({**observation, "invocation_id": value["invocation_id"],
                             "workflow_id": TaskDescription.from_dict(value["task"]).workflow_id})
    return observations
