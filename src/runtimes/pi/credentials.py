"""Bind saved Pi subscription credentials without granting the login file.

Only the trusted experiment parent reads/refreshed the normal SDK login store.
The inherited child binding is in memory and the bridge consumes a private
descriptor before creating any Agent tools. Ordinary Pi sessions keep their
existing SDK-managed credential-file behavior.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

_CREDENTIALS: ContextVar[dict | None] = ContextVar("agentloom_pi_private_credentials", default=None)
_LIMIT = 1024 * 1024


@contextmanager
def bind_pi_credentials(value):
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("Private Pi credentials must be a provider mapping")
    token = _CREDENTIALS.set(None if value is None else deepcopy(dict(value)))
    try:
        yield
    finally:
        _CREDENTIALS.reset(token)


def current_pi_credentials():
    value = _CREDENTIALS.get()
    return None if value is None else deepcopy(value)


def _read(path, providers):
    if not path.is_file():
        raise ValueError("Pi subscription login is missing; log in through Pi before running the Application")
    with path.open("rb") as stream:
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("Pi credential store exceeds its private transport capacity")
    try:
        value = json.loads(raw)
        selected = {provider: value[provider] for provider in providers}
        if any(not isinstance(item, dict) or item.get("type") != "oauth" for item in selected.values()):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Pi subscription login is unavailable; refresh the Pi login before running the Application") from None
    return selected


def capture_pi_credentials(model_catalog):
    """Capture only the subscription providers required by the frozen catalog.

    Refresh an expiring login in the trusted parent first. The SDK persists its
    rotated credential in the existing protected store, avoiding independent
    trial copies racing refresh-token rotation. Values never enter an archive.
    """
    if _CREDENTIALS.get() is not None:
        return current_pi_credentials()
    catalog = dict(model_catalog or {})
    models = catalog.get("models") or catalog.get("model") or {}
    providers = set()
    for item in models.values():
        if not isinstance(item, Mapping):
            continue
        adapter = item.get("adapter")
        if adapter == "openai_codex_responses":
            providers.add("openai-codex")
        elif adapter == "openai_chatgpt_responses":
            providers.add("openai")
    if not providers:
        return {}
    path = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi/agent") / "auth.json"
    selected = _read(path, providers)
    expiring = any(type(item.get("expires")) not in {int, float}
                   or item["expires"] < (time.time() + 10 * 60) * 1000 for item in selected.values())
    if expiring:
        from .environment import build_pi_subprocess_env
        from .install import find_node, installed_pi_entry

        env = build_pi_subprocess_env()
        node = find_node(env)
        package = installed_pi_entry(node=node).parents[1] / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
        script = """
const {ModelRuntime} = await import(process.argv[1]);
const runtime = await ModelRuntime.create({authPath:process.argv[2],modelsPath:null,refreshOnCreate:false});
for (const provider of JSON.parse(process.argv[3])) {
  const auth = await runtime.getAuth(provider);
  if (auth?.source !== 'OAuth') process.exit(2);
}
"""
        try:
            refreshed = subprocess.run([node, "--input-type=module", "-e", script,
                package.as_uri(), str(path), json.dumps(sorted(providers))],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("Pi subscription credential refresh failed before Application execution") from None
        if refreshed.returncode:
            raise ValueError("Pi subscription credential refresh failed; refresh the Pi login before running the Application")
        selected = _read(path, providers)
    return selected


@contextmanager
def pi_credential_descriptor():
    """One bridge-only anonymous descriptor; never expose values in env/argv."""
    value = current_pi_credentials()
    if value is None:
        yield None
        return
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("Private Pi credentials require a validated anonymous descriptor backend")
    import fcntl
    fd = os.memfd_create("agentloom-pi-auth", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        payload = json.dumps(value, allow_nan=False).encode()
        if len(payload) > _LIMIT:
            raise ValueError("Private Pi credentials exceed their bounded transport")
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(payload)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        yield fd
    finally:
        os.close(fd)
