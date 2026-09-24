"""Required, Task-scoped execution evidence and its Python inspection API."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from agentloom.execution.context import RuntimeContext
from agentloom.execution.storage import SecureDirectory
from agentloom.execution.tool_protocol import ToolCallRecord
from agentloom.execution.trace import capture_explicit_execution_context
from agentloom.self_learning.redaction import redact_value

_CURRENT_RECORDER: ContextVar[TraceRecorder | None] = ContextVar(
    "agentloom_trace_recorder", default=None
)


class TraceStorageError(RuntimeError):
    """Required local evidence could not be written or verified."""


class RunWithTrace(Protocol):
    run_id: str
    trace_dir: Path | None


class TraceRecorder:
    """Persist one Run's facts in its logical Task's immutable content store."""

    def __init__(self, context: RuntimeContext) -> None:
        self.context = context
        self.storage = SecureDirectory(context.prepare_trace())
        self._lock = threading.RLock()

    def close(self) -> None:
        self.storage.close()

    def _payload(self, value: Any, *, content_type: str) -> str:
        if content_type == "application/json":
            data = json.dumps(
                redact_value(value), ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
        else:
            data = str(redact_value(value)).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        reference = f"payload_{uuid4().hex}"
        self.storage.atomic_write(f"payloads/{digest}.blob", data)
        self.storage.atomic_write_json(
            f"refs/{reference}.json",
            {"sha256": digest, "size": len(data), "content_type": content_type},
        )
        return reference

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock, self.storage.advisory_file_lock("sequence.lock", create=True):
            try:
                current = json.loads(self.storage.read_bytes("sequence.json"))
                sequence = int(current["last"]) + 1
            except FileNotFoundError:
                sequence = 1
            self.storage.atomic_write_json("sequence.json", {"last": sequence})
            event["sequence"] = sequence
            event["schema_version"] = 1
            event["run_id"] = self.context.run_id
            event["task_id"] = self.context.task_id
            event["application_id"] = self.context.application_id
            self.storage.atomic_write_json(
                f"events/{self.context.run_id}/{sequence:012d}.json", event
            )

    def record_tool(self, record: ToolCallRecord) -> None:
        execution = capture_explicit_execution_context()
        try:
            input_ref = self._payload(record.input, content_type="application/json")
            output_ref = (
                self._payload(record.output, content_type="application/json")
                if record.status == "completed" else None
            )
            model_ref = self._payload(record.model_content(), content_type="text/plain")
            self._append({
                "kind": "tool",
                "call_id": record.call_id,
                "tool_name": record.tool_name,
                "status": record.status,
                "agent_id": execution.local_run_id,
                "parent_agent_id": execution.hook_run.parent.local_run_id
                if execution.hook_run is not None and execution.hook_run.parent is not None else None,
                "step_number": execution.hook_run.step_number
                if execution.hook_run is not None else None,
                "started_at": record.started_at,
                "ended_at": record.ended_at,
                "error": redact_value(asdict(record.error)) if record.error is not None else None,
                "input_ref": input_ref,
                "output_ref": output_ref,
                "model_ref": model_ref,
            })
        except Exception as exc:
            raise TraceStorageError(
                f"Could not persist Tool trace for call {record.call_id}: {exc}"
            ) from exc


@contextmanager
def bind_trace_recorder(context: RuntimeContext) -> Iterator[TraceRecorder]:
    recorder = TraceRecorder(context)
    token = _CURRENT_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT_RECORDER.reset(token)
        recorder.close()


def get_current_trace_recorder() -> TraceRecorder | None:
    return _CURRENT_RECORDER.get()


@dataclass(slots=True)
class RunTrace:
    """Read one Run's metadata and integrity-checked retained content."""

    storage: SecureDirectory
    run_id: str

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> RunTrace:
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def events(self) -> list[dict[str, Any]]:
        directory = self.storage.path / "events" / self.run_id
        if not directory.is_dir():
            return []
        return [
            json.loads(self.storage.read_bytes(f"events/{self.run_id}/{path.name}"))
            for path in sorted(directory.glob("*.json"))
        ]

    def read_text(self, reference: str) -> str:
        if not reference.startswith("payload_") or len(reference) != 40:
            raise ValueError("Invalid trace payload reference")
        metadata = json.loads(self.storage.read_bytes(f"refs/{reference}.json"))
        digest = metadata["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise TraceStorageError("Invalid trace payload digest")
        data = self.storage.read_bytes(f"payloads/{digest}.blob")
        if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != digest:
            raise TraceStorageError(f"Trace payload integrity check failed: {reference}")
        return data.decode("utf-8")


def inspect_run(run: RunWithTrace) -> RunTrace:
    """Open retained evidence for one public Application Run receipt."""

    if run.trace_dir is None:
        raise ValueError("Run does not expose a trace directory")
    return RunTrace(SecureDirectory(run.trace_dir, create=False), run.run_id)
