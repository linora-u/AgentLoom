"""Required, Task-scoped execution evidence and its Python inspection API."""

from __future__ import annotations

import hashlib
import json
import re
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
_UNSET = object()
_TRACE_REF_RE = re.compile(r"^(?:payload_[0-9a-f]{32}|ctx_[0-9a-f]{32})$")


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

    def _payload(
        self, value: Any, *, content_type: str, prefix: str = "payload",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if content_type == "application/json":
            data = json.dumps(
                redact_value(value), ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
        else:
            data = str(redact_value(value)).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        reference = f"{prefix}_{uuid4().hex}"
        self.storage.atomic_write(f"payloads/{digest}.blob", data)
        self.storage.atomic_write_json(
            f"refs/{reference}.json",
            {"sha256": digest, "size": len(data), "content_type": content_type,
             **(metadata or {})},
        )
        return reference

    def project_tool_result(self, text: str, *, tool_name: str, source: str, call_id: str) -> str:
        """Make a durable, bounded Model projection for a large text result."""

        from agentloom.execution.context_engine.runtime import get_active_context_engine

        safe_text = str(redact_value(text))
        if tool_name == "loom_retrieve_context":
            return safe_text
        engine = get_active_context_engine()
        threshold = engine.config.min_chars if engine is not None else 32768
        preview_limit = engine.config.preview_max_chars if engine is not None else 2048
        if len(safe_text.encode("utf-8")) < threshold:
            return safe_text
        execution = capture_explicit_execution_context()
        try:
            reference = self._payload(
                safe_text, content_type="text/plain", prefix="ctx",
                metadata={
                    "tool_name": tool_name, "source": source, "call_id": call_id,
                    "agent_path": execution.runtime_agent_path,
                    "producing_run_id": self.context.run_id,
                },
            )
        except Exception as exc:
            raise TraceStorageError(f"Could not persist large Tool result for {call_id}: {exc}") from exc
        preview = safe_text[:max(1, preview_limit)]
        size = len(safe_text.encode("utf-8"))
        return (
            f"[ContextRef {reference} source={tool_name} size_bytes={size}]\n"
            f'Use loom_retrieve_context(ref="{reference}", offset=0, limit=8192) '
            "to read more bytes.\n\n" + preview
        )

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

    def record_tool(self, record: ToolCallRecord, *, original_output: Any = _UNSET) -> None:
        execution = capture_explicit_execution_context()
        try:
            input_ref = self._payload(record.input, content_type="application/json")
            output_ref = (
                self._payload(
                    record.output if original_output is _UNSET else original_output,
                    content_type="application/json",
                )
                if record.status == "completed" or original_output is not _UNSET else None
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

    def record_model_request(self, request: Any, *, runtime: str, boundary: str) -> str:
        execution = capture_explicit_execution_context()
        turn_id = f"model_{uuid4().hex}"
        try:
            request_ref = self._payload(request, content_type="application/json")
            self._append({
                "kind": "model_request",
                "runtime": runtime,
                "boundary": boundary,
                "model_turn_id": turn_id,
                "agent_id": execution.local_run_id,
                "parent_agent_id": execution.hook_run.parent.local_run_id
                if execution.hook_run is not None and execution.hook_run.parent is not None else None,
                "step_number": execution.hook_run.step_number
                if execution.hook_run is not None else None,
                "request_ref": request_ref,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist Model request trace: {exc}") from exc
        return turn_id

    def record_model_response(
        self, turn_id: str, response: Any = _UNSET, *, runtime: str, error: BaseException | None = None
    ) -> None:
        try:
            response_ref = self._payload(response, content_type="application/json") if response is not _UNSET else None
            self._append({
                "kind": "model_response",
                "runtime": runtime,
                "model_turn_id": turn_id,
                "status": "error" if error is not None else "completed",
                "response_ref": response_ref,
                "error": str(redact_value(str(error))) if error is not None else None,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist Model response trace: {exc}") from exc


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
class TracePage:
    data: bytes
    next_offset: int | None
    total_bytes: int
    content_type: str


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

    def _metadata(self, reference: str) -> dict[str, Any]:
        if not _TRACE_REF_RE.fullmatch(reference):
            raise ValueError("Invalid trace payload reference")
        metadata = json.loads(self.storage.read_bytes(f"refs/{reference}.json"))
        digest = metadata["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise TraceStorageError("Invalid trace payload digest")
        return metadata

    def reference_metadata(self, reference: str) -> dict[str, Any]:
        """Return validated metadata for an opaque retained payload reference."""

        return dict(self._metadata(reference))

    def read_text(self, reference: str) -> str:
        metadata = self._metadata(reference)
        digest = metadata["sha256"]
        data = self.storage.read_bytes(f"payloads/{digest}.blob")
        if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != digest:
            raise TraceStorageError(f"Trace payload integrity check failed: {reference}")
        return data.decode("utf-8")

    def read_page(self, reference: str, *, offset: int = 0, limit: int = 8192) -> TracePage:
        """Read at most 64 KiB by byte offset, verifying the whole payload."""

        if offset < 0 or not 1 <= limit <= 65536:
            raise ValueError("Trace page requires offset >= 0 and 1 <= limit <= 65536")
        metadata = self._metadata(reference)
        digest = metadata["sha256"]
        with self.storage.open_binary_reader(f"payloads/{digest}.blob") as stream:
            checksum = hashlib.sha256()
            total = 0
            while chunk := stream.read(1024 * 1024):
                checksum.update(chunk)
                total += len(chunk)
            if total != metadata["size"] or checksum.hexdigest() != digest:
                raise TraceStorageError(f"Trace payload integrity check failed: {reference}")
            stream.seek(offset)
            data = stream.read(limit)
        next_offset = offset + len(data)
        return TracePage(
            data=data,
            next_offset=next_offset if next_offset < total else None,
            total_bytes=total,
            content_type=str(metadata["content_type"]),
        )


def inspect_run(run: RunWithTrace) -> RunTrace:
    """Open retained evidence for one public Application Run receipt."""

    if run.trace_dir is None:
        raise ValueError("Run does not expose a trace directory")
    return RunTrace(SecureDirectory(run.trace_dir, create=False), run.run_id)
