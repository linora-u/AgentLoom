"""Required, Task-scoped execution evidence and its Python inspection API."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from agentloom.execution.context import RuntimeContext
from agentloom.execution.storage import SecureDirectory
from agentloom.execution.tool_protocol import ToolCallRecord
from agentloom.execution.trace import capture_explicit_execution_context
from agentloom.execution.trace_export import (
    AsyncTraceExport,
    TraceExporter,
    record_export_diagnostic,
)
from agentloom.self_learning.redaction import redact_value

_CURRENT_RECORDER: ContextVar[TraceRecorder | None] = ContextVar(
    "agentloom_trace_recorder", default=None
)
_UNSET = object()
_TRACE_REF_RE = re.compile(r"^(?:payload_[0-9a-f]{32}|ctx_[0-9a-f]{32})$")
_CONTEXT_REF_RE = re.compile(r"\[ContextRef (ctx_[0-9a-f]{32})\b")
_RUN_DIR_RE = re.compile(r"^run_[A-Za-z0-9_-]+$")


def _utf8_chunks(pieces: Iterable[str]) -> Iterator[bytes]:
    """Bound the temporary encoded buffer even for one huge JSON string."""

    for piece in pieces:
        for start in range(0, len(piece), 65536):
            yield piece[start:start + 65536].encode("utf-8")


class TraceStorageError(RuntimeError):
    """Required local evidence could not be written or verified."""


class RunWithTrace(Protocol):
    run_id: str
    trace_dir: Path | None


class TraceRecorder:
    """Persist one Run's facts in its logical Task's immutable content store."""

    def __init__(self, context: RuntimeContext, *, exporter: TraceExporter | None = None) -> None:
        self.context = context
        self.storage = SecureDirectory(context.prepare_trace())
        self._lock = threading.RLock()
        self._run_steps: dict[tuple[str, int], int] = {}
        self._model_steps: dict[str, int | None] = {}
        self._next_run_step = 1
        self._presenter: Any | None = None
        self._export: AsyncTraceExport | None = None
        if exporter is not None:
            try:
                self._export = AsyncTraceExport(exporter, self.storage)
            except Exception as exc:
                record_export_diagnostic(self.storage, "start_failed", None, str(exc))

    def attach_presenter(self, presenter: Any) -> None:
        """Attach the run's human-readable projection after its logger exists."""

        self._presenter = presenter

    @property
    def has_presenter(self) -> bool:
        return self._presenter is not None

    def close(self) -> None:
        if self._export is not None:
            self._export.close()
        self.storage.close()

    def _payload(
        self, value: Any, *, content_type: str, prefix: str = "payload",
        metadata: dict[str, Any] | None = None,
        redacted: bool = False,
    ) -> str:
        safe = value if redacted else redact_value(value)
        if content_type == "application/json":
            pieces = json.JSONEncoder(
                ensure_ascii=False, separators=(",", ":"), default=str
            ).iterencode(safe)
        else:
            pieces = iter((str(safe),))
        digest, size = self.storage.write_content_addressed(
            "payloads", _utf8_chunks(pieces)
        )
        reference = f"{prefix}_{uuid4().hex}"
        self.storage.atomic_write_json(
            f"refs/{reference}.json",
            {"sha256": digest, "size": size, "content_type": content_type,
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
        threshold = min(engine.config.min_chars if engine is not None else 32768, 131072)
        preview_limit = min(engine.config.preview_max_chars if engine is not None else 2048, 16384)
        size = sum(len(chunk) for chunk in _utf8_chunks(iter((safe_text,))))
        if size < threshold:
            return safe_text
        execution = capture_explicit_execution_context()
        try:
            reference = self._payload(
                safe_text, content_type="text/plain", prefix="ctx", redacted=True,
                metadata={
                    "tool_name": tool_name, "source": source, "call_id": call_id,
                    "agent_path": execution.runtime_agent_path,
                    "producing_run_id": self.context.run_id,
                },
            )
        except Exception as exc:
            raise TraceStorageError(f"Could not persist large Tool result for {call_id}: {exc}") from exc
        preview = safe_text[:max(1, preview_limit)]
        return (
            f"[ContextRef {reference} source={tool_name} size_bytes={size}]\n"
            f'Use loom_retrieve_context(ref="{reference}", offset=0, limit=8192) '
            "to read more bytes.\n\n" + preview
        )

    def _append(self, event: dict[str, Any]) -> None:
        with self._lock:
            with self.storage.advisory_file_lock("sequence.lock", create=True):
                try:
                    current = json.loads(self.storage.read_bytes("sequence.json"))
                    sequence = int(current["last"]) + 1
                except FileNotFoundError:
                    sequence = 1
                self.storage.atomic_write_json("sequence.json", {"last": sequence})
                event["sequence"] = sequence
                event["event_id"] = f"{self.context.run_id}:{sequence}"
                event["schema_version"] = 1
                event["recorded_at"] = datetime.now(UTC).isoformat()
                event["run_id"] = self.context.run_id
                event["task_id"] = self.context.task_id
                event["application_id"] = self.context.application_id
                self.storage.atomic_write_json(
                    f"events/{self.context.run_id}/{sequence:012d}.json", event
                )
            if self._presenter is not None:
                self._presenter.consume(event)
            if self._export is not None:
                try:
                    self._export.enqueue(event)
                except Exception as exc:
                    record_export_diagnostic(self.storage, "enqueue_failed", event.get("event_id"), str(exc))

    def _run_step_number(
        self, agent_id: str | None, agent_step_number: int | None, *, create: bool = False,
    ) -> int | None:
        if not agent_id or not isinstance(agent_step_number, int) or agent_step_number < 1:
            return None
        key = (agent_id, agent_step_number)
        with self._lock:
            if key not in self._run_steps and create:
                self._run_steps[key] = self._next_run_step
                self._next_run_step += 1
            return self._run_steps.get(key)

    def record_agent_start(self, *, task: str | None, agent_name: str, runtime: str) -> None:
        """Mark one Agent invocation before its runtime starts its first turn."""

        execution = capture_explicit_execution_context()
        try:
            task_ref = self._payload(task or "", content_type="text/plain")
            self._append({
                "kind": "agent_start",
                "agent_id": execution.local_run_id,
                "parent_agent_id": execution.hook_run.parent.local_run_id
                if execution.hook_run is not None and execution.hook_run.parent is not None else None,
                "agent_name": agent_name,
                "runtime": runtime,
                "task_ref": task_ref,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist Agent start trace: {exc}") from exc

    def record_agent_end(self, *, state: str, error: BaseException | None = None) -> None:
        execution = capture_explicit_execution_context()
        agent_step = execution.hook_run.step_number if execution.hook_run is not None else None
        try:
            self._append({
                "kind": "agent_end",
                "agent_id": execution.local_run_id,
                "step_number": agent_step,
                "run_step_number": self._run_step_number(execution.local_run_id, agent_step),
                "state": state,
                "error": str(redact_value(str(error))) if error is not None else None,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist Agent end trace: {exc}") from exc

    def record_final_answer(self, output: Any) -> None:
        """Record the accepted Application result after Run finalization."""

        try:
            rendered = output if isinstance(output, str) else json.dumps(
                redact_value(output), ensure_ascii=False, indent=2, default=str
            )
            answer_ref = self._payload(rendered, content_type="text/plain")
            self._append({"kind": "final_answer", "answer_ref": answer_ref})
        except Exception as exc:
            raise TraceStorageError(f"Could not persist final answer trace: {exc}") from exc

    def record_tool(self, record: ToolCallRecord, *, original_output: Any = _UNSET) -> None:
        execution = capture_explicit_execution_context()
        agent_step = execution.hook_run.step_number if execution.hook_run is not None else None
        try:
            input_ref = self._payload(record.input, content_type="application/json")
            output_ref = (
                self._payload(
                    record.output if original_output is _UNSET else original_output,
                    content_type="application/json",
                )
                if record.status == "completed" or original_output is not _UNSET else None
            )
            model_ref = self._payload(record.model_content(), content_type="text/plain", redacted=True)
            self._append({
                "kind": "tool",
                "call_id": record.call_id,
                "tool_name": record.tool_name,
                "status": record.status,
                "agent_id": execution.local_run_id,
                "parent_agent_id": execution.hook_run.parent.local_run_id
                if execution.hook_run is not None and execution.hook_run.parent is not None else None,
                "step_number": agent_step,
                "run_step_number": self._run_step_number(execution.local_run_id, agent_step),
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

    def record_hook_decision(
        self, *, event: str, subject: str, input_value: Any,
        decision: Any, call_id: str | None = None,
    ) -> None:
        execution = capture_explicit_execution_context()
        agent_step = execution.hook_run.step_number if execution.hook_run is not None else None
        try:
            decision_ref = self._payload(
                {"input": input_value, "result": asdict(decision)},
                content_type="application/json",
            )
            self._append({
                "kind": "hook_decision",
                "event": event,
                "subject": subject,
                "call_id": call_id,
                "agent_id": execution.local_run_id,
                "parent_agent_id": execution.hook_run.parent.local_run_id
                if execution.hook_run is not None and execution.hook_run.parent is not None else None,
                "step_number": agent_step,
                "run_step_number": self._run_step_number(execution.local_run_id, agent_step),
                "decision_ref": decision_ref,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist {event} Hook decision: {exc}") from exc

    def record_model_request(
        self, request: Any, *, runtime: str, boundary: str, attempt: int | None = None,
        provider_request_complete: bool = False,
    ) -> str:
        execution = capture_explicit_execution_context()
        agent_step = execution.hook_run.step_number if execution.hook_run is not None else None
        turn_id = f"model_{uuid4().hex}"
        try:
            run_step = self._run_step_number(execution.local_run_id, agent_step, create=True)
            with self._lock:
                self._model_steps[turn_id] = run_step
            request_ref = self._payload(request, content_type="application/json")
            self._append({
                "kind": "model_request",
                "runtime": runtime,
                "boundary": boundary,
                "provider_request_complete": provider_request_complete,
                "model_turn_id": turn_id,
                "attempt": attempt,
                "agent_id": execution.local_run_id,
                "parent_agent_id": execution.hook_run.parent.local_run_id
                if execution.hook_run is not None and execution.hook_run.parent is not None else None,
                "step_number": agent_step,
                "run_step_number": run_step,
                "request_ref": request_ref,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist Model request trace: {exc}") from exc
        return turn_id

    def record_model_response(
        self, turn_id: str, response: Any = _UNSET, *, runtime: str,
        error: BaseException | None = None, attempt: int | None = None,
    ) -> None:
        execution = capture_explicit_execution_context()
        agent_step = execution.hook_run.step_number if execution.hook_run is not None else None
        try:
            response_ref = self._payload(response, content_type="application/json") if response is not _UNSET else None
            usage = None
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                raw_usage = response["usage"]
                if runtime == "pi":
                    usage = {
                        "input_tokens": sum(int(raw_usage.get(key) or 0) for key in ("input", "cacheRead", "cacheWrite")),
                        "output_tokens": int(raw_usage.get("output") or 0),
                    }
                else:
                    usage = {
                        "input_tokens": int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0),
                        "output_tokens": int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0),
                    }
            self._append({
                "kind": "model_response",
                "runtime": runtime,
                "model_turn_id": turn_id,
                "attempt": attempt,
                "agent_id": execution.local_run_id,
                "step_number": agent_step,
                "run_step_number": self._model_steps.get(turn_id),
                "status": "error" if error is not None else "completed",
                "response_ref": response_ref,
                "usage": usage,
                "error": str(redact_value(str(error))) if error is not None else None,
            })
        except Exception as exc:
            raise TraceStorageError(f"Could not persist Model response trace: {exc}") from exc


@contextmanager
def bind_trace_recorder(
    context: RuntimeContext, *, exporter: TraceExporter | None = None,
) -> Iterator[TraceRecorder]:
    recorder = TraceRecorder(context, exporter=exporter)
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
class TraceSearchPage:
    matches: list[tuple[int, str]]
    next_offset: int | None
    total_bytes: int


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

    def verify_committed_context_refs(self) -> None:
        """Reject a resume if a previously committed Tool reference is broken."""

        events_dir = self.storage.path / "events"
        if not events_dir.is_dir():
            return
        verified: set[str] = set()
        for run_dir in sorted(events_dir.iterdir()):
            if not run_dir.is_dir() or not _RUN_DIR_RE.fullmatch(run_dir.name):
                continue
            for event_path in sorted(run_dir.glob("*.json")):
                event = json.loads(self.storage.read_bytes(f"events/{run_dir.name}/{event_path.name}"))
                if event.get("kind") != "tool" or event.get("status") != "completed":
                    continue
                model_ref = event.get("model_ref")
                if not isinstance(model_ref, str):
                    continue
                for reference in _CONTEXT_REF_RE.findall(self.read_text(model_ref)):
                    if reference in verified:
                        continue
                    try:
                        self.read_page(reference, limit=1)
                    except (OSError, ValueError, KeyError) as exc:
                        raise TraceStorageError(f"Committed Tool reference is unavailable: {reference}") from exc
                    verified.add(reference)

    def inspect_step(
        self, run_step_number: int, *, max_inline_bytes: int = 65536,
    ) -> dict[str, Any]:
        """Expand one Run Step, leaving large bodies available through read_page."""

        if run_step_number < 1 or max_inline_bytes < 0:
            raise ValueError("Step inspection requires a positive Run Step and byte limit")
        selected = [
            event for event in self.events()
            if event.get("run_step_number") == run_step_number
        ]
        if not selected:
            raise KeyError(f"No retained Run Step {run_step_number}")
        agent_id = next((event["agent_id"] for event in selected if event.get("agent_id")), None)
        references = {
            value for event in selected for key, value in event.items()
            if key.endswith("_ref") and isinstance(value, str)
        }
        sizes = sorted(
            (self._metadata(reference)["size"], reference) for reference in references
        )
        remaining = max_inline_bytes
        payloads: dict[str, str] = {}
        for size, reference in sizes:
            if size > remaining:
                continue
            payloads[reference] = self.read_text(reference)
            remaining -= size
        return {
            "agent_id": agent_id,
            "run_step_number": run_step_number,
            "events": selected,
            "payload_refs": sorted(references),
            "payloads": payloads,
        }

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

    def search_page(
        self, reference: str, query: str, *, offset: int = 0, limit: int = 5,
    ) -> TraceSearchPage:
        """Search at most 1 MiB of one retained payload, using byte offsets."""

        needle = query.encode("utf-8")
        if not 1 <= len(needle) <= 256 or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Trace search requires a short query, byte offset and 1..100 matches")
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
            if offset >= total:
                return TraceSearchPage([], None, total)

            scan_end = min(total, offset + 1024 * 1024)
            stream.seek(offset)
            window = stream.read(scan_end - offset + len(needle) - 1)
            matches: list[tuple[int, str]] = []
            cursor = 0
            while len(matches) < limit:
                found = window.find(needle, cursor)
                if found < 0 or offset + found >= scan_end:
                    break
                position = offset + found
                start = max(0, position - 128)
                end = min(total, position + len(needle) + 256)
                stream.seek(start)
                excerpt = stream.read(end - start)
                before = excerpt.rfind(b"\n", 0, position - start)
                if before >= 0:
                    excerpt = excerpt[before + 1:]
                    start += before + 1
                after = excerpt.find(b"\n", position - start + len(needle))
                if after >= 0:
                    excerpt = excerpt[:after]
                matches.append((position, excerpt.decode("utf-8", errors="replace")))
                cursor = found + len(needle)
            next_offset = offset + cursor if len(matches) == limit else scan_end
            return TraceSearchPage(
                matches,
                next_offset if next_offset < total else None,
                total,
            )


def inspect_run(run: RunWithTrace) -> RunTrace:
    """Open retained evidence for one public Application Run receipt."""

    if run.trace_dir is None:
        raise ValueError("Run does not expose a trace directory")
    return RunTrace(SecureDirectory(run.trace_dir, create=False), run.run_id)
