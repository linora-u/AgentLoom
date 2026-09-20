"""Install the locked Node SDK for Pi; usable through uv or the loom CLI."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess

from agentloom.adapters.pi.metadata import SDK_VERSION
from agentloom.runtime.subprocess_env import build_subprocess_env

SDK_PACKAGE = "@earendil-works/pi-coding-agent"


def _fingerprint(bridge: Path) -> str:
    manifest = json.loads((bridge / "package.json").read_text())
    lock = json.loads((bridge / "package-lock.json").read_text())
    if (manifest["dependencies"][SDK_PACKAGE] != SDK_VERSION
            or lock["packages"][""]["dependencies"][SDK_PACKAGE] != SDK_VERSION
            or lock["packages"]["node_modules/" + SDK_PACKAGE]["version"] != SDK_VERSION):
        raise RuntimeError("Pi SDK version and its committed dependency lock do not match.")
    inputs = [bridge / name for name in ("package.json", "package-lock.json", "tsconfig.json", "index.ts", "protocol.ts")]
    # Include modules added by later runtime tickets without including SDK code.
    inputs.extend(path for path in sorted(bridge.glob("*.ts")) if path not in inputs)
    schemas = sorted(bridge.parent.glob("bridge-v*.schema.json"))
    if not schemas:
        raise RuntimeError("Pi bridge protocol schema is missing from the installation.")
    inputs.extend(schemas)
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def installed_pi_entry(bridge: Path | None = None) -> Path:
    """Check an existing build without downloading or mutating dependencies."""
    bridge = bridge or Path(__file__).parent / "bridge"
    try:
        if _installed(bridge, _fingerprint(bridge)):
            return bridge / "dist/index.js"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        pass
    raise RuntimeError("Pi SDK is not installed or its build is stale/incomplete. Run loom install-runtime pi (uv run --locked loom install-runtime pi in a checkout).")


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


def _installed(bridge: Path, fingerprint: str) -> bool:
    try:
        stamp = json.loads((bridge / ".agentloom-install.json").read_text())
        package = json.loads((bridge / "node_modules" / SDK_PACKAGE / "package.json").read_text())
        return (stamp == {"fingerprint": fingerprint, "sdk_version": SDK_VERSION}
                and package["version"] == SDK_VERSION
                and all((bridge / "dist" / source.with_suffix(".js").name).is_file()
                        for source in bridge.glob("*.ts")))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def install_pi(bridge: Path | None = None) -> Path:
    """Download the exact lockfile dependencies and build our own bridge.

    Repeated calls reuse a successful installation. A process lock serializes
    callers; an interrupted or failed install never receives a ready marker.
    ``bridge`` permits isolated installation verification without touching the
    running package's dependency directory.
    """
    bridge = (bridge or Path(__file__).parent / "bridge").resolve()
    env = build_subprocess_env()
    for name in list(env):
        if name.startswith(("PI_", "NODE_")):
            env.pop(name)
    node = find_node(env)
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    try:
        fingerprint = _fingerprint(bridge)
        with (bridge / ".agentloom-install.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if _installed(bridge, fingerprint):
                return bridge / "dist/index.js"
            npm = shutil.which("npm", path=env["PATH"])
            if not npm:
                raise RuntimeError("Pi installation requires npm alongside Node.js.")
            stamp = bridge / ".agentloom-install.json"
            stamp.unlink(missing_ok=True)
            _run([npm, "ci", "--ignore-scripts", "--include=dev", "--no-audit", "--no-fund"], bridge, env)
            _run([node, str(bridge / "node_modules/typescript/bin/tsc")], bridge, env)
            _run([node, "--input-type=module", "-e", f"await import('{SDK_PACKAGE}'); await import('./dist/protocol.js');"], bridge, env)
            installed = json.loads((bridge / "node_modules" / SDK_PACKAGE / "package.json").read_text())
            if installed["version"] != SDK_VERSION or not all(
                (bridge / "dist" / source.with_suffix(".js").name).is_file()
                for source in bridge.glob("*.ts")
            ):
                raise RuntimeError("Pi installation did not produce the locked SDK and bridge.")
            stamp.write_text(json.dumps({"fingerprint": fingerprint, "sdk_version": SDK_VERSION}) + "\n")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise RuntimeError("Cannot prepare Pi dependencies. Check Node/npm, network access and write access to the Pi directory.") from None
    return bridge / "dist/index.js"


def main() -> None:
    try:
        entry = install_pi()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Pi SDK {SDK_VERSION} ready: {entry}")


if __name__ == "__main__":
    main()
