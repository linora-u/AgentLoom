"""One isolated JSON operation under AgentLoom's existing native guardian."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from uuid import uuid4
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from .isolation import preflight_isolation, trial_spec
from .snapshot import CandidateBundle


class IsolatedExecutionError(RuntimeError):
    category = "infrastructure"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _atomic(path, value):
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".pending")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _context_dict(context):
    value = context.to_dict() if hasattr(context, "to_dict") else dataclasses.asdict(context) if dataclasses.is_dataclass(context) else dict(context)
    return {key: str(item) if isinstance(item, Path) else item for key, item in value.items()}


def execute_isolated(bundle: CandidateBundle, context, factory: str, payload: Mapping, *, stop: threading.Event | None = None, timeout: float | None = None, read_paths: Sequence[Path] = (), denied_paths: Sequence[Path] = (), environment: Mapping[str, str] | None = None, model_config: Mapping | None = None, credentials: Mapping | None = None) -> dict:
    """Execute ``factory(payload, bundle_descriptor, context_descriptor)`` once.

    Private model config crosses only the child's anonymous stdin pipe and is
    consumed/closed before factory import. It never enters identity or disk.
    Repeated calls with a completed identical operation reuse its result.
    """
    return _execute(bundle, context, factory, payload, stop=stop, timeout=timeout, read_paths=read_paths, denied_paths=denied_paths, environment=environment, model_config=model_config, credentials=credentials, boundary="trial")


def execute_trusted_orchestration(bundle, context, factory, payload, **options):
    """Fixed news orchestration whose actual Agents run in isolated Hosts.

    This internal seam is not selectable in YAML. It cannot run roles or an
    arbitrary driver; every NewsHost must install the frozen mandatory policy.
    """
    if factory != "agentloom.app.harnessing.controller:execute_trial" or payload.get("driver") != "news_agent.runtime.harnessing:NewsApplicationDriver":
        raise ValueError("Trusted orchestration is restricted to the built-in managed news driver")
    return _execute(bundle, context, factory, payload, boundary="managed_host", **options)


def _execute(bundle, context, factory, payload, *, stop=None, timeout=None, read_paths=(), denied_paths=(), environment=None, model_config=None, credentials=None, boundary):
    from agentloom.execution.managed_process import launch_owned_process, recover_owned_process

    preflight_isolation()
    bundle.verify()
    if not set(credentials or {}) <= set(bundle.manifest["credential_refs"]):
        raise ValueError("Private capability binding was not declared by this candidate")
    if not isinstance(factory, str) or ":" not in factory or any(part.startswith("_") for part in factory.split(":")):
        raise ValueError("Isolated factory must be an importable public module:function")
    for key, value in (environment or {}).items():
        if key not in {"PATH", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}:
            raise ValueError("Unsupported inherited Harness environment capability: " + key)
        # Environment forms part of the public execution identity. A proxy
        # credential must use a private capability, never a persisted URL.
        if key.endswith("_PROXY") and key != "NO_PROXY":
            url = urlsplit(value)
            if url.username is not None or url.password is not None or url.query or url.fragment:
                raise ValueError("Credential-bearing proxy URLs cannot be archived by Harness")
    descriptor = _context_dict(context)
    if descriptor["candidate_id"] != bundle.candidate_id:
        raise ValueError("Trial candidate identity differs from bundle")
    root = Path(descriptor["root"]).resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise ValueError("Trial cannot use the user's root or home as its state directory")
    if root == bundle.root or root.is_relative_to(bundle.root) or bundle.root.is_relative_to(root):
        raise ValueError("Trial state and immutable bundle must be disjoint")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("workspace", "runtime", "business", "output", "sessions", "tmp", "home"):
        (root / name).mkdir(exist_ok=True)
    job = root / "process"
    job.mkdir(exist_ok=True)
    input_value = {"schema_version": 1, "factory": factory, "payload": dict(payload), "bundle": bundle.to_dict(), "context": descriptor, "execution_boundary": boundary,
                   "read_paths": sorted(str(Path(path).resolve()) for path in read_paths),
                   "denied_paths": sorted(str(Path(path).resolve()) for path in denied_paths), "environment": dict(environment or {})}
    identity = hashlib.sha256(_json(input_value).encode()).hexdigest()
    input_value["identity"] = identity
    input_path, result_path = job / "input.json", job / "result.json"
    # A held flock excludes concurrent replacement and is released by kernel
    # on controller loss. Resource recovery remains the existing owner API.
    import fcntl
    with (job / "owner.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IsolatedExecutionError("Another controller owns this Trial operation") from error
        if input_path.exists():
            if json.loads(input_path.read_text()) != input_value:
                raise ValueError("Trial operation identity already belongs to different input")
            for resource in sorted((job / "attempts").glob("*/resource")):
                if not (resource / "description.json").exists():
                    continue
                previous = recover_owned_process(resource)
                if not previous.observe()["physical_exit_confirmed"]:
                    previous.request_stop()
                    previous.wait_exited(timeout=20)
            if result_path.exists():
                return _result(result_path, identity)
        else:
            _atomic(input_path, input_value)
        attempts = job / "attempts"
        attempts.mkdir(exist_ok=True)
        attempt = attempts / f"{len(tuple(attempts.iterdir())) + 1:06d}"
        attempt.mkdir()
        # Requested access is part of the trusted driver contract. It cannot be
        # changed by candidate YAML or arbitrary include_paths in a tool policy.
        spec = trial_spec(bundle.root, descriptor, read_paths=read_paths, denied_paths=denied_paths, environment=environment)
        import agentloom.execution.managed_process as managed_module
        guardian_entry = Path(managed_module.__file__).with_name("_process_guardian.py").resolve()
        expected_guardian = bundle.manifest["files"]["python/agentloom/execution/_process_guardian.py"]["hash"]
        if "sha256:" + hashlib.sha256(guardian_entry.read_bytes()).hexdigest() != expected_guardian:
            raise ValueError("Native guardian source differs from frozen framework dependency")
        launch = {**input_value, "isolation": spec.to_dict(), "guardian_entry": str(guardian_entry)}
        _atomic(attempt / "launch.json", launch)
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
               "PYTHONPATH": os.pathsep.join((str(bundle.root / "python"), str(bundle.project_root), str(bundle.project_root / "applications")))}
        for key, value in (environment or {}).items():
            if key not in {"PATH", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}:
                raise ValueError("Unsupported inherited Harness environment capability: " + key)
            env[key] = value
        from agentloom.runtimes.pi.credentials import current_pi_credentials
        private = _json({"model_config": model_config, "credentials": dict(credentials or {}), "pi_credentials": current_pi_credentials()}).encode() + b"\n"
        if len(private) > 4 * 1024 * 1024:
            raise ValueError("Private model configuration exceeds one-shot transport limit")
        bootstrap = bundle.root / "python" / "agentloom" / "app" / "harnessing" / "bootstrap.py"
        if not bootstrap.is_file():
            raise IsolatedExecutionError("Candidate lacks trusted isolated bootstrap")
        started = time.monotonic()
        owner = None
        try:
            with (attempt / "stdout.log").open("wb") as stdout, (attempt / "stderr.log").open("wb") as stderr:
                owner = launch_owned_process([sys.executable, "-B", str(bootstrap), str(attempt)], env=env, cwd=str(bundle.project_root), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, receipt_dir=attempt / "resource", gated=True)
                owner.release()
                # Reader consumes this before any candidate factory import.
                owner.stdin.write(private)
                owner.stdin.close()
                while owner.poll() is None:
                    if stop is not None and stop.is_set():
                        owner.request_stop()
                        raise InterruptedError("Harness operation stopped")
                    if timeout is not None and time.monotonic() - started >= timeout:
                        owner.request_stop()
                        raise TimeoutError("Harness operation timed out")
                    time.sleep(.02)
                owner.wait_exited(timeout=20)
            _atomic(attempt / "cleanup.json", owner.observe())
            actual_result = attempt / "result.json"
            if not actual_result.exists():
                raise IsolatedExecutionError("Isolated operation exited without a result; inspect its stderr receipt")
            result = _result(actual_result, identity)
            _atomic(result_path, {"identity": identity, "result": result})
            return result
        finally:
            if owner is not None:
                owner.close()
                _atomic(attempt / "cleanup.json", owner.observe())


def _result(path, identity):
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as error:
        raise IsolatedExecutionError("Isolated operation has no complete JSON receipt; inspect its stderr receipt") from error
    if value.get("identity") != identity:
        raise IsolatedExecutionError("Isolated operation result has another identity")
    if "error" in value:
        error = value["error"]
        frames = error.get("frames", [])
        location = f" at {frames[-1]['file']}:{frames[-1]['line']}" if frames else ""
        raise IsolatedExecutionError("Isolated operation failed: " + error["type"] + ": " + error["message"] + location)
    result = value.get("result")
    if not isinstance(result, dict):
        raise IsolatedExecutionError("Isolated operation must return a JSON mapping")
    return result
