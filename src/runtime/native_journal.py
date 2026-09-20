"""Run-scoped native call snapshots, atomically committed before acknowledgement.

The existing SecureDirectory supplies file+directory fsync, no-follow storage
and interprocess locking. Raw output, provenance and terminal record share one
snapshot so a durable acknowledgement never points at a partially saved result.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from agentloom.runtime.native_tools import (
    NativeAuthorization,
    NativeCallIdentity,
    NativeCommitAck,
    NativeJournalEntry,
    NativePrepareRequest,
    ToolManifestEntry,
)
from agentloom.runtime.storage import SecureDirectory
from agentloom.runtime.tool_protocol import ToolCallRecord


def snapshot(value: Any) -> Any:
    """Detach contract mappings and reject non-JSON output before persistence."""
    return json.loads(
        json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            default=lambda item: dict(item) if isinstance(item, Mapping) else asdict(item),
        )
    )


def journal_entry(data: dict[str, Any]) -> NativeJournalEntry:
    request = data["request"]
    identity = NativeCallIdentity(**request["identity"])
    tool = ToolManifestEntry(**request["tool"])
    grant = NativeAuthorization(data["authorization_id"], identity, tool, request["cwd"], data["final_arguments"])
    ack = None
    if data.get("record") is not None:
        ack = NativeCommitAck(
            identity, grant.authorization_id, data["commit_id"], ToolCallRecord.from_dict(data["record"])
        )
    return NativeJournalEntry(
        grant, data["state"], NativePrepareRequest(identity, tool, request["cwd"], request["raw_arguments"]), ack
    )


class NativeCallJournal:
    def __init__(self, directory: Path):
        self.directory = directory
        self._storage = SecureDirectory(directory)
        self._lock = RLock()
        try:
            # atomic_write fsyncs each snapshot and its containing directory.
            # Also persist newly created ancestor entries; otherwise a reboot
            # could lose the whole journal despite a successful file fsync.
            for parent in directory.resolve(strict=True).parents:
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            self._storage.close()
            raise

    def artifact_path(self, identity: NativeCallIdentity) -> Path:
        """Stable reference to the snapshot containing the original raw output."""
        key = hashlib.sha256(json.dumps([identity.instance_id, identity.call_id]).encode()).hexdigest()
        return self.directory / f"{key}.json"

    @contextmanager
    def transaction(self, identity: NativeCallIdentity, *, confirm: bool = False) -> Iterator[dict[str, Any]]:
        # Parent/session anchors must match the stored identity, not form a new
        # namespace that would permit reuse of an already consumed call ID.
        name = self.artifact_path(identity).name
        key = Path(name).stem
        with self._lock, self._storage.advisory_file_lock(f"{key}.lock", create=True):
            try:
                data = self._storage.read_json(name)
            except FileNotFoundError:
                data = {}
            before = snapshot(data)
            yield data
            if data != before or (confirm and data):
                self._storage.atomic_write_json(name, snapshot(data))

    def close(self) -> None:
        self._storage.close()
