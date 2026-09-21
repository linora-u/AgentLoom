"""Install the locked Node SDK for Pi; usable through uv or the loom CLI."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from agentloom.runtimes.pi.metadata import BRIDGE_VERSION, SDK_VERSION
from agentloom.runtime.subprocess_env import build_subprocess_env

SDK_PACKAGE = "@earendil-works/pi-coding-agent"
PROTOCOL_VERSION = 2
READY_MANIFEST = "ready.json"


def source_bridge_dir() -> Path:
    """Return the read-only bridge source shipped with AgentLoom."""

    return Path(__file__).parent / "bridge"


def pi_runtime_root() -> Path:
    """Return the active development environment's Pi runtime asset root."""

    return Path(sys.prefix).resolve() / "share" / "pi"


def _fingerprint(bridge: Path) -> str:
    manifest = json.loads((bridge / "package.json").read_text())
    lock = json.loads((bridge / "package-lock.json").read_text())
    if (manifest["dependencies"][SDK_PACKAGE] != SDK_VERSION
            or lock["packages"][""]["dependencies"][SDK_PACKAGE] != SDK_VERSION
            or lock["packages"]["node_modules/" + SDK_PACKAGE]["version"] != SDK_VERSION):
        raise RuntimeError("Pi SDK version and its committed dependency lock do not match.")
    inputs = [bridge / name for name in ("package.json", "package-lock.json", "tsconfig.json")]
    # Include every owned bridge source, excluding downloaded and built code.
    inputs.extend(sorted(path for path in bridge.rglob("*.ts")
                         if not {"node_modules", "dist"} & set(path.relative_to(bridge).parts)))
    schemas = sorted(bridge.parent.glob("bridge-v*.schema.json"))
    if not schemas:
        raise RuntimeError("Pi bridge protocol schema is missing from the installation.")
    inputs.extend(schemas)
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(str(path.relative_to(bridge.parent)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _node_abi(node: str, env: dict[str, str]) -> str:
    try:
        probe = subprocess.run(
            [node, "-p", "process.versions.modules"],
            env=env,
            capture_output=True,
            timeout=5,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Cannot inspect the selected Node.js runtime.") from None
    abi = probe.stdout.strip()
    if not abi:
        raise RuntimeError("Cannot inspect the selected Node.js runtime.")
    return abi


def _identity(source_bridge: Path, *, node: str, env: dict[str, str]) -> dict[str, str]:
    return {
        "sdk_version": SDK_VERSION,
        "bridge_version": str(BRIDGE_VERSION),
        "protocol_version": str(PROTOCOL_VERSION),
        "fingerprint": _fingerprint(source_bridge),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "node_abi": _node_abi(node, env),
    }


def _ready(runtime_root: Path, identity: dict[str, str]) -> bool:
    try:
        manifest = json.loads((runtime_root / READY_MANIFEST).read_text())
        bridge = runtime_root / "bridge"
        package = json.loads((bridge / "node_modules" / SDK_PACKAGE / "package.json").read_text())
        return (
            manifest.get("identity") == identity
            and manifest.get("entry") == "bridge/dist/index.js"
            and package["version"] == SDK_VERSION
            and all((bridge / "dist" / source.with_suffix(".js").name).is_file()
                    for source in bridge.glob("*.ts"))
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def installed_pi_entry(
    runtime_root: Path | None = None,
    *,
    source_bridge: Path | None = None,
    node: str | None = None,
) -> Path:
    """Check an existing build without downloading or mutating dependencies."""

    env = build_subprocess_env()
    for name in list(env):
        if name.startswith(("PI_", "NODE_")):
            env.pop(name)
    node = node or find_node(env)
    source_bridge = (source_bridge or source_bridge_dir()).resolve()
    runtime_root = (runtime_root or pi_runtime_root()).resolve()
    try:
        identity = _identity(source_bridge, node=node, env=env)
        if _ready(runtime_root, identity):
            return runtime_root / "bridge" / "dist" / "index.js"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        pass
    raise RuntimeError("Pi SDK is not installed or its build is stale/incomplete. Run loom runtime install pi (uv run --locked loom runtime install pi in a checkout).")


def find_node(env: dict[str, str]) -> str:
    """Skip the Node 18 executable bundled for other AgentLoom tools."""
    inspected = set()
    for directory in os.get_exec_path(env):
        candidate = shutil.which("node", path=directory)
        if candidate is None or candidate in inspected:
            continue
        inspected.add(candidate)
        try:
            probe = subprocess.run([candidate, "--version"], env=env,
                capture_output=True, timeout=5, text=True, check=True)
            version = re.fullmatch(r"v(\d+)\.(\d+)\.\d+\s*", probe.stdout)
            if version and (int(version[1]), int(version[2])) >= (22, 19):
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError("Pi requires Node >=22.19. Install a compatible Node.js runtime first.")


def _run(command: list[str], bridge: Path, env: dict[str, str]) -> None:
    # Installation is explicit, outside Agent execution. Do not expose npm
    # output that can contain private registry credentials or environment data.
    with subprocess.Popen(command, cwd=bridge, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True) as process:
        try:
            process.communicate(timeout=300)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        if process.returncode:
            raise RuntimeError(f"Pi dependency installation failed during {Path(command[0]).name} (exit {process.returncode}).")


def _copy_inputs(source_bridge: Path, staging_root: Path) -> Path:
    destination_bridge = staging_root / "bridge"
    ignore = shutil.ignore_patterns("node_modules", "dist", ".agentloom-install.*")
    shutil.copytree(source_bridge, destination_bridge, ignore=ignore)
    schemas = sorted(source_bridge.parent.glob("bridge-v*.schema.json"))
    if not schemas:
        raise RuntimeError("Pi bridge protocol schema is missing from the installation.")
    for schema in schemas:
        shutil.copy2(schema, staging_root / schema.name)
    return destination_bridge


def _publish(staging_root: Path, runtime_root: Path) -> None:
    backup_root = runtime_root.parent / f".pi-old-{os.getpid()}-{next(tempfile._get_candidate_names())}"
    if runtime_root.exists():
        os.rename(runtime_root, backup_root)
    try:
        os.rename(staging_root, runtime_root)
    except BaseException:
        if backup_root.exists():
            os.rename(backup_root, runtime_root)
        raise
    if backup_root.exists():
        shutil.rmtree(backup_root)


def install_pi(source_bridge: Path | None = None, runtime_root: Path | None = None) -> Path:
    """Download the exact lockfile dependencies and build our own bridge.

    Repeated calls reuse a successful installation. A process lock serializes
    callers; an interrupted or failed install never replaces a ready runtime.
    ``source_bridge`` and ``runtime_root`` permit isolated verification without
    touching the active development environment's assets.
    """
    source_bridge = (source_bridge or source_bridge_dir()).resolve()
    runtime_root = (runtime_root or pi_runtime_root()).resolve()
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    env = build_subprocess_env()
    for name in list(env):
        if name.startswith(("PI_", "NODE_")):
            env.pop(name)
    node = find_node(env)
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    identity = _identity(source_bridge, node=node, env=env)
    lock = runtime_root.parent / ".pi-install.lock"
    staging_root: Path | None = None
    try:
        with lock.open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if _ready(runtime_root, identity):
                return runtime_root / "bridge" / "dist" / "index.js"
            npm = shutil.which("npm", path=env["PATH"])
            if not npm:
                raise RuntimeError("Pi installation requires npm alongside Node.js.")
            staging_root = Path(tempfile.mkdtemp(prefix=".pi-staging-", dir=runtime_root.parent))
            bridge = _copy_inputs(source_bridge, staging_root)
            _run([npm, "ci", "--ignore-scripts", "--include=dev", "--no-audit", "--no-fund"], bridge, env)
            _run([node, str(bridge / "node_modules/typescript/bin/tsc")], bridge, env)
            _run([node, "--input-type=module", "-e", f"await import('{SDK_PACKAGE}'); await import('./dist/protocol.js');"], bridge, env)
            installed = json.loads((bridge / "node_modules" / SDK_PACKAGE / "package.json").read_text())
            if installed["version"] != SDK_VERSION or not all(
                (bridge / "dist" / source.with_suffix(".js").name).is_file()
                for source in bridge.glob("*.ts")
            ):
                raise RuntimeError("Pi installation did not produce the locked SDK and bridge.")
            (staging_root / READY_MANIFEST).write_text(
                json.dumps(
                    {"identity": identity, "entry": "bridge/dist/index.js"},
                    sort_keys=True,
                )
                + "\n"
            )
            _publish(staging_root, runtime_root)
            staging_root = None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        raise RuntimeError(f"Cannot prepare Pi dependencies: {exc}") from exc
    return runtime_root / "bridge" / "dist" / "index.js"


def pi_runtime_status(runtime_root: Path | None = None, *, source_bridge: Path | None = None) -> dict[str, object]:
    env = build_subprocess_env()
    for name in list(env):
        if name.startswith(("PI_", "NODE_")):
            env.pop(name)
    runtime_root = (runtime_root or pi_runtime_root()).resolve()
    source_bridge = (source_bridge or source_bridge_dir()).resolve()
    try:
        node = find_node(env)
        identity = _identity(source_bridge, node=node, env=env)
        ready = _ready(runtime_root, identity)
        state = "ready" if ready else ("stale_or_corrupt" if runtime_root.exists() else "missing")
        return {"runtime": "pi", "state": state, "root": str(runtime_root), "identity": identity}
    except RuntimeError as exc:
        return {"runtime": "pi", "state": "error", "root": str(runtime_root), "error": str(exc)}


def uninstall_pi(runtime_root: Path | None = None) -> str:
    runtime_root = (runtime_root or pi_runtime_root()).resolve()
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
        return str(runtime_root)
    return str(runtime_root)


def main() -> None:
    try:
        entry = install_pi()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Pi SDK {SDK_VERSION} ready: {entry}")


if __name__ == "__main__":
    main()
