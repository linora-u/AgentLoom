"""OpenClaw's public process SDK behind a control-pipe transport.

No native signals, descendant census, leases or retries live in this Adapter.
The surviving Node transport requests SDK cancellation on host-channel EOF.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

from .files import atomic_json, read_json
from .os_job import LaunchdJobs, OwnedJobs, ResourceOwnershipError, process_alive

OPENCLAW_VERSION = "2026.10.3"


class OpenClawJobs(OwnedJobs):
    owner = "openclaw"

    def __init__(self, root, host, *, runtime_root: Path | None = None, node: str | None = None):
        if sys.platform not in {"darwin", "linux"}:
            raise RuntimeError("OpenClaw task ownership requires macOS or Linux")
        super().__init__(root, host)
        if runtime_root is None or node is None:
            from .openclaw_install import installed_owner_runtime, managed_node
            runtime_root = runtime_root or installed_owner_runtime()
            node = node or managed_node()
        runtime = Path(runtime_root).resolve(strict=True)
        package = runtime / "node_modules" / "openclaw"
        manifest = read_json(package / "package.json")
        if manifest.get("version") != OPENCLAW_VERSION:
            raise ValueError(f"Process ownership requires OpenClaw {OPENCLAW_VERSION}")
        self.runtime = runtime
        self.node = str(Path(node).resolve(strict=True))
        version = subprocess.run([self.node, "--version"], check=True, text=True,
                                 capture_output=True, timeout=5).stdout.strip()
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
        parsed = tuple(map(int, match.groups())) if match else ()
        if not (parsed and ((parsed[0] == 24 and parsed >= (24, 16, 0))
                            or parsed >= (26, 1, 0))):
            raise ValueError("OpenClaw requires Node >=24.16.0 <25 or >=26.1.0")
        # Preflight outside DBOS admission. Import failures cannot leave a task
        # waiting for an SDK receipt that was never capable of being produced.
        subprocess.run([self.node, "--input-type=module", "-e",
                        "import {createRequire} from 'node:module';"
                        "import {pathToFileURL} from 'node:url';import {join} from 'node:path';"
                        "const r=createRequire(join(process.argv[1],'package.json'));"
                        "await import(pathToFileURL(r.resolve('openclaw/plugin-sdk/process-runtime')));",
                        str(runtime)], start_new_session=True,
                       check=True, capture_output=True, timeout=20)
        self.children: dict[Path, subprocess.Popen] = {}

    def _start(self, job: Path):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(str(Path(p or ".").resolve()) for p in sys.path)
        bridge = Path(__file__).with_name("openclaw_bridge.mjs")
        try:
            with (job / "guardian.log").open("wb") as log:
                child = subprocess.Popen([self.node, str(bridge), str(self.runtime), str(job), sys.executable],
                    stdin=subprocess.PIPE, stdout=log, stderr=log, env=env, start_new_session=True,
                    close_fds=True)
        except OSError:
            # No transport was admitted. There is no native scope to drain.
            atomic_json(job / "exited.json", {"physical_exit_confirmed": True, "not_started": True})
            raise
        self.children[job] = child
        atomic_json(job / "guardian.json", {"pid": child.pid,
            "creation": psutil.Process(child.pid).create_time(),
            "executor": {"pid": os.getpid(), "creation": psutil.Process().create_time()},
            "engine_version": OPENCLAW_VERSION})

    def _terminate(self, job: Path):
        owner = read_json(job / "description.json").get("owner", "launchd")
        if owner == "launchd":
            # Previously loaded jobs still belong to the old native owner.
            LaunchdJobs(self.root, self.host_spec)._terminate(job)
            return
        if owner != self.owner:
            raise ResourceOwnershipError("Unknown invocation process owner")
        child = self.children.get(job)
        if child is not None and child.stdin is not None:
            child.stdin.close()  # SDK cancellation, not a host-side tree kill.
            child.stdin = None
        identity_path = job / "guardian.json"
        identity = read_json(identity_path) if identity_path.exists() else None
        if child is None and identity and process_alive(identity["executor"]):
            raise ResourceOwnershipError("Invocation belongs to a live executor")
        deadline = time.monotonic() + 30
        while not (job / "exited.json").exists():
            if (job / "openclaw-cleanup-failed.json").exists():
                raise ResourceOwnershipError("OpenClaw could not certify cleanup; see openclaw-cleanup-failed.json")
            if time.monotonic() >= deadline:
                raise ResourceOwnershipError("OpenClaw cleanup receipt did not arrive; see guardian.log")
            time.sleep(0.02)
        if read_json(job / "exited.json").get("physical_exit_confirmed") is not True:
            raise ResourceOwnershipError("OpenClaw exit receipt is invalid")
        while identity and process_alive(identity):
            if time.monotonic() >= deadline:
                raise ResourceOwnershipError("OpenClaw transport did not exit after cleanup")
            time.sleep(0.02)
        if child is not None:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
            self.children.pop(job, None)
