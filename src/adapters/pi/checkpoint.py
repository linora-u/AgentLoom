"""Durable Pi session artifacts and transport receipts owned by one task.

The SDK interprets its messages. This module persists opaque snapshots before
execution and reads the host's original journal when a new Run reconciles them.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agentloom.adapters.pi.metadata import SDK_VERSION
from agentloom.runtime.agent_runtime import (
    AgentRuntimeError, RuntimeCheckpointEnvelope, RuntimeDefinition,
)
from agentloom.runtime.context import RuntimeContext
from agentloom.runtime.native_journal import recovery_receipt, snapshot
from agentloom.runtime.native_tools import NativeCallIdentity
from agentloom.runtime.storage import SecureDirectory
from agentloom.runtime.tool_protocol import ToolCallRecord

STATE_VERSION = 1
MAX_SESSION_BYTES = 128 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _configuration_key(definition: RuntimeDefinition) -> str:
    # Credentials, request headers, fixed inputs and provider settings are not
    # persisted or fingerprinted. The current Run supplies fresh credentials.
    model = definition.model_selection
    value = {
        "name": definition.name,
        "model": [model.model_id, model.protocol] if model else None,
        "tools": [{"name": tool.visible_name, "provider": tool.provider,
                   "owner": tool.owner, "parameters": dict(tool.parameters)}
                  for tool in definition.tool_manifest],
        "cwd": str(Path(definition.project_root or ".").resolve()),
        "instructions": definition.instructions,
        "runtime_options": dict(definition.runtime_options),
    }
    return _digest(json.dumps(value, sort_keys=True).encode())


class PiCheckpointStore:
    def __init__(self, runtime: RuntimeContext, definition: RuntimeDefinition,
                 private_directory: Path, instance_id: str,
                 sink: Callable[[RuntimeCheckpointEnvelope], None]):
        runtime.prepare_checkpoint()
        self.runtime = runtime
        self.definition = definition
        self.instance_id = instance_id
        self.configuration_key = _configuration_key(definition)
        self.private = SecureDirectory(private_directory, create=False)
        parent = SecureDirectory(runtime.validate_checkpoint_path(require_exists=True), create=False)
        try:
            self.storage = parent.child("pi")
        finally:
            parent.close()
        self.sink = sink
        self.latest: RuntimeCheckpointEnvelope | None = None
        self._lock = RLock()

    def _read(self, storage: SecureDirectory, name: str, digest: str) -> tuple[bytes, dict[str, Any]]:
        if not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid Pi session artifact identity")
        raw, truncated = storage.read_bytes_up_to(name, MAX_SESSION_BYTES)
        if truncated or _digest(raw) != digest:
            raise ValueError("Pi session artifact is incomplete or changed")
        bundle = json.loads(raw)
        if not isinstance(bundle, dict) or set(bundle) != {"version", "sdk_version", "scope", "session", "calls", "phase"}:
            raise ValueError("Unsupported Pi session artifact")
        if bundle["version"] != STATE_VERSION or bundle["sdk_version"] != SDK_VERSION:
            raise ValueError("Incompatible Pi session artifact version")
        scope = bundle["scope"]
        if not isinstance(scope, dict) or (scope.get("application_id"), scope.get("task_id")) != (
                self.runtime.application_id, self.runtime.task_id):
            raise ValueError("Pi session belongs to another Application or task")
        session = bundle["session"]
        if (not isinstance(session, dict) or set(session) != {"header", "entries"}
                or not isinstance(session["header"], dict)
                or session["header"].get("type") != "session"
                or session["header"].get("version") != 3
                or not isinstance(session["header"].get("id"), str)
                or not isinstance(session["entries"], list)
                or not isinstance(bundle["calls"], list)
                or bundle["phase"] not in {"running", "complete"}):
            raise ValueError("Invalid Pi native session structure")
        if session["header"].get("cwd") != str(Path(self.definition.project_root or ".").resolve()):
            raise ValueError("Pi session workspace has changed")
        seen = set()
        for call in bundle["calls"]:
            if (not isinstance(call, dict) or set(call) != {"identity", "tool_name", "arguments", "owner"}
                    or not isinstance(call["identity"], dict) or not isinstance(call["arguments"], dict)):
                raise ValueError("Invalid Pi session call descriptor")
            identity = NativeCallIdentity(**call["identity"])
            self._require_identity(identity, session["header"]["id"])
            key = (identity.native_parent_id, identity.call_id)
            if key in seen:
                raise ValueError("Duplicate Pi session call identity")
            seen.add(key)
        return raw, bundle

    def _require_identity(self, identity: NativeCallIdentity, session_id: str | None = None) -> None:
        if (identity.application_id, identity.task_id) != (self.runtime.application_id, self.runtime.task_id):
            raise ValueError("Pi receipt belongs to another task")
        if session_id is not None and identity.native_session_id != session_id:
            raise ValueError("Pi receipt belongs to another native session")

    def save(self, digest: str) -> RuntimeCheckpointEnvelope:
        with self._lock:
            raw, bundle = self._read(self.private, f"session-{digest}.json", digest)
            if (bundle["scope"].get("instance_id"), bundle["scope"].get("run_id")) != (
                    self.instance_id, self.runtime.run_id):
                raise ValueError("Pi snapshot instance or Run mismatch")
            self.storage.atomic_write(f"sessions/{digest}.json", raw)
            for call in bundle["calls"]:
                identity = NativeCallIdentity(**call["identity"])
                if call["owner"] != "runtime" and identity.run_id == self.runtime.run_id:
                    if identity.instance_id != self.instance_id:
                        raise ValueError("Pi platform preparation belongs to another instance")
                    self.prepare_platform(identity, call["tool_name"], call["arguments"])
            checkpoint = RuntimeCheckpointEnvelope(
                runtime_id="pi", runtime_version=SDK_VERSION, state_schema_version=STATE_VERSION,
                task_id=self.runtime.task_id, run_id=self.runtime.run_id,
                progress=len(bundle["session"]["entries"]),
                payload={"artifact": digest, "configuration": self.configuration_key,
                         "session_id": bundle["session"]["header"]["id"],
                         "source_instance_id": self.instance_id},
            )
            # Returning from this callback is the execution barrier. A sink
            # failure must propagate; an observer notification is insufficient.
            self.sink(checkpoint)
            self.latest = checkpoint
            return checkpoint

    def load(self, checkpoint: RuntimeCheckpointEnvelope) -> dict[str, Any]:
        checkpoint.require_compatible(runtime_id="pi", runtime_version=SDK_VERSION,
                                      state_schema_version=STATE_VERSION)
        if checkpoint.task_id != self.runtime.task_id or checkpoint.payload.get("configuration") != self.configuration_key:
            raise AgentRuntimeError("Pi checkpoint task or definition is incompatible", category="configuration")
        digest = checkpoint.payload.get("artifact")
        if not isinstance(digest, str):
            raise ValueError("Pi checkpoint has no session artifact")
        _, bundle = self._read(self.storage, f"sessions/{digest}.json", digest)
        if (bundle["session"]["header"]["id"] != checkpoint.payload.get("session_id")
                or bundle["scope"].get("run_id") != checkpoint.run_id
                or bundle["scope"].get("instance_id") != checkpoint.payload.get("source_instance_id")):
            raise ValueError("Pi checkpoint does not match its native artifact")
        return bundle

    def native_receipt(self, identity: NativeCallIdentity) -> dict[str, Any]:
        """Read old-Run evidence without rebinding it to a new call identity."""
        return recovery_receipt(self.runtime, identity)

    def _platform_key(self, identity: NativeCallIdentity) -> str:
        self._require_identity(identity)
        return "platform/" + _digest(json.dumps(snapshot(identity), sort_keys=True).encode()) + ".json"

    def platform_receipt(self, identity: NativeCallIdentity, *, recovering: bool = False) -> dict[str, Any]:
        with self._lock:
            data = self.storage.read_json(self._platform_key(identity))
            if data.get("identity") != snapshot(identity):
                raise ValueError("Pi platform receipt identity mismatch")
            state = data.get("state")
            if state not in {"prepared", "executing", "uncertain", "committed"}:
                raise ValueError("Pi platform receipt has an unknown state")
            if (state == "committed") != (data.get("record") is not None):
                raise ValueError("Pi platform receipt state does not match its terminal record")
            if state == "committed":
                record = ToolCallRecord.from_dict(data["record"])
                if record.call_id != identity.call_id or record.tool_name != data["tool_name"]:
                    raise ValueError("Pi platform receipt result identity mismatch")
            if recovering and data.get("state") == "executing":
                data["state"] = "uncertain"
                self.storage.atomic_write_json(self._platform_key(identity), data)
            return data

    def prepare_platform(self, identity: NativeCallIdentity, tool_name: str, arguments: dict[str, Any]) -> None:
        with self._lock:
            key = self._platform_key(identity)
            expected = {"identity": snapshot(identity), "tool_name": tool_name, "arguments": arguments}
            try:
                existing = self.storage.read_json(key)
            except FileNotFoundError:
                self.storage.atomic_write_json(key, {**expected, "state": "prepared"})
            else:
                if any(existing.get(k) != v for k, v in expected.items()):
                    raise ValueError("Pi platform preparation changed")

    def start_platform(self, identity: NativeCallIdentity, tool_name: str, arguments: dict[str, Any]) -> None:
        with self._lock:
            data = self.platform_receipt(identity)
            if data["state"] != "prepared" or data["tool_name"] != tool_name or data["arguments"] != arguments:
                raise ValueError("Pi platform callback already dispatched")
            self.storage.atomic_write_json(self._platform_key(identity), {**data, "state": "executing"})

    def commit_platform(self, identity: NativeCallIdentity, record: ToolCallRecord) -> None:
        with self._lock:
            data = self.platform_receipt(identity)
            if data["state"] != "executing" or record.call_id != identity.call_id or record.tool_name != data["tool_name"]:
                raise ValueError("Pi platform commit does not match dispatched call")
            self.storage.atomic_write_json(self._platform_key(identity),
                                           {**data, "state": "committed", "record": record.to_dict()})

    def close(self) -> None:
        self.private.close()
        self.storage.close()
