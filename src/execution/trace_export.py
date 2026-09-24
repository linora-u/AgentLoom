"""Optional, bounded delivery of committed trace facts to external consumers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any, Protocol

from agentloom.execution.storage import SecureDirectory
from agentloom.self_learning.redaction import redact_value


class TraceExporter(Protocol):
    """Consume metadata and references without making a Run wait for delivery."""

    def export(self, event: Mapping[str, Any], *, trace_dir: Path) -> None: ...


def record_export_diagnostic(
    storage: SecureDirectory, kind: str, event_id: Any, message: str,
) -> None:
    """Keep exporter faults separate from the Run's mandatory trace writes."""

    try:
        storage.append_text(
            "exporter/diagnostics.jsonl",
            json.dumps({
                "recorded_at": datetime.now(UTC).isoformat(),
                "kind": kind,
                "event_id": event_id,
                "message": redact_value(message),
            }, ensure_ascii=False) + "\n",
        )
    except Exception:
        pass


class AsyncTraceExport:
    """Best-effort notifications; durable facts remain readable in trace_dir."""

    def __init__(self, exporter: TraceExporter, storage: SecureDirectory) -> None:
        self._exporter = exporter
        self._storage = storage.duplicate()
        self._queue: Queue[dict[str, Any]] = Queue(maxsize=256)
        self._closing = Event()
        self._thread = Thread(target=self._drain, name="agentloom-trace-export", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._storage.close()
            raise

    def enqueue(self, event: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(deepcopy(event))
        except Full:
            record_export_diagnostic(self._storage, "queue_full", event.get("event_id"), "Trace export queue is full")

    def close(self) -> None:
        self._closing.set()
        self._thread.join(timeout=0.5)
        if self._thread.is_alive():
            record_export_diagnostic(self._storage, "pending", None, "Trace export continues after Run completion")

    def _drain(self) -> None:
        try:
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.05)
                except Empty:
                    continue
                try:
                    self._exporter.export(event, trace_dir=self._storage.path)
                except BaseException as exc:
                    record_export_diagnostic(self._storage, "export_failed", event.get("event_id"), str(exc))
                finally:
                    self._queue.task_done()
        finally:
            self._storage.close()
