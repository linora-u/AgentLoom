"""Install immutable, locked SDK assets without launching an OpenClaw Gateway."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from filelock import FileLock

from .files import atomic_json, read_json
from .openclaw_job import OPENCLAW_VERSION


def managed_node() -> str:
    try:
        from nodejs_wheel import node
    except ImportError:
        raise RuntimeError("Install AgentLoom[durable] to provision the pinned Node runtime") from None
    result = node(["-p", "process.execPath"], return_completed_process=True,
                  check=True, capture_output=True, text=True, timeout=5)
    return result.stdout.strip()


def asset_identity():
    source = Path(__file__).with_name("owner_assets")
    return {"engine_version": OPENCLAW_VERSION, "node_version": "24.16.0",
            "lock_sha256": hashlib.sha256((source / "package-lock.json").read_bytes()).hexdigest(),
            "platform": sys.platform, "machine": platform.machine()}


def owner_runtime_root() -> Path:
    identity = asset_identity()
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    return Path(sys.prefix) / "share" / "agentloom-process-owner" / key


def installed_owner_runtime() -> Path:
    root = owner_runtime_root()
    try:
        if read_json(root / "ready.json") == asset_identity():
            return root
    except (OSError, ValueError):
        pass
    raise RuntimeError("OpenClaw process assets are not installed. Run loom runtime install execution-owner")


def install_openclaw() -> Path:
    node = managed_node()
    from nodejs_wheel import npm
    root = owner_runtime_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(root.parent / "install.lock"):
        if root.exists():
            return installed_owner_runtime()
        source = Path(__file__).with_name("owner_assets")
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=root.parent))
        try:
            for name in ("package.json", "package-lock.json"):
                shutil.copy2(source / name, staging / name)
            env = os.environ.copy()
            env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
            # No install scripts, no implicit lifecycle code, no unpinned resolve.
            result = npm(["ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund"],
                return_completed_process=True, cwd=staging, env=env,
                capture_output=True, timeout=300)
            if result.returncode:
                raise RuntimeError(f"Locked OpenClaw installation failed (exit {result.returncode})")
            subprocess.run([node, "--input-type=module", "-e",
                "const p=await import('openclaw/plugin-sdk/process-runtime');"
                "if(typeof p.runCommandWithTimeout !== 'function' || typeof p.withCommandProcessScope !== 'function') process.exit(1);"],
                cwd=staging, env=env, capture_output=True, check=True, timeout=20)
            atomic_json(staging / "ready.json", asset_identity())
            os.rename(staging, root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return root
