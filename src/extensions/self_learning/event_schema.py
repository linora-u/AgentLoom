"""Canonical self-learning event schema."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .redaction import redact_mapping, require_safe_identity, sanitize_text_fragment

_MAX_CONTENT_CHARS = 60000
_IDENTITY_FIELDS = (
    "event_id",
    "run_id",
    "root_run_id",
    "task_id",
    "parent_task_id",
    "parent_event_id",
)
_TEXT_FIELDS = (
    "application_id",
    "application_name",
    "application_path",
    "workflow_path",
    "agent_name",
    "worker_name",
    "event_type",
    "phase",
    "source",
    "role",
    "tool_name",
    "content_ref",
    "status",
    "created_at",
    "source_path",
)


def require_strict_int(value: Any, *, field: str) -> int:
    """Return an integer without allowing SQLite-affinity coercions.

    ``bool`` is deliberately rejected even though it subclasses ``int`` in
    Python.  Keeping the error value-free also prevents an invalid secret-like
    string from being copied into logs by exception handlers.
    """
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    return value


def require_optional_strict_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return require_strict_int(value, field=field)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def safe_run_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Validate before replacing punctuation.  Reversing this order can turn a
    # detectable credential such as ``password=secret`` into
    # ``password_secret`` and permanently hide it from the safety boundary.
    raw = require_safe_identity(raw, field="run identity")
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    return cleaned[:120]


@dataclass(frozen=True)
class CanonicalSessionEvent:
    """Append-only record used as the self-learning source of truth."""

    event_id: str
    run_id: str
    schema_version: int = 3
    root_run_id: str = ""
    task_id: str = ""
    parent_task_id: str = ""
    parent_event_id: str = ""
    application_id: str = ""
    application_name: str = ""
    application_path: str = ""
    workflow_path: str = ""
    agent_name: str = ""
    worker_name: str = ""
    event_type: str = ""
    phase: str = ""
    source: str = "hook"
    role: str = ""
    tool_name: str = ""
    content: str = ""
    content_text: str = ""
    content_ref: str = ""
    status: str = ""
    step_number: int | None = None
    created_at: str = ""
    source_path: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_optional_strict_int(
            self.step_number,
            field="event step_number",
        )

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        # Revalidate at the storage boundary as well as construction time.  A
        # frozen dataclass can still be altered through low-level Python APIs;
        # no such value may reach SQLite's permissive INTEGER affinity.
        record["step_number"] = require_optional_strict_int(
            record.get("step_number"),
            field="event step_number",
        )
        record["root_run_id"] = record.get("root_run_id") or record.get("run_id") or ""
        for key in _IDENTITY_FIELDS:
            record[key] = safe_run_id(record.get(key, ""))
        for key in _TEXT_FIELDS:
            record[key] = sanitize_text_fragment(record.get(key, ""))
        record["content"] = sanitize_text_fragment(
            record.get("content", ""), max_chars=_MAX_CONTENT_CHARS
        )
        record["content_text"] = sanitize_text_fragment(
            record.get("content_text", "") or record.get("content", ""),
            max_chars=_MAX_CONTENT_CHARS,
        )
        record["input_data"] = redact_mapping(record.get("input_data") or {})
        record["output_data"] = redact_mapping(record.get("output_data") or {})
        record["metadata"] = redact_mapping(record.get("metadata") or {})
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CanonicalSessionEvent:
        values = dict(record)
        values.setdefault("event_id", uuid.uuid4().hex)
        values["run_id"] = safe_run_id(str(values.get("run_id") or ""))
        if not values["run_id"]:
            raise ValueError("run_id is required")
        values.setdefault("created_at", now_iso())
        values.setdefault("source", "hook")
        values.setdefault("schema_version", 3)
        values["root_run_id"] = safe_run_id(
            str(values.get("root_run_id") or values["run_id"])
        )
        values["content"] = sanitize_text_fragment(
            values.get("content", ""), max_chars=_MAX_CONTENT_CHARS
        )
        values["content_text"] = sanitize_text_fragment(
            values.get("content_text", "") or values.get("content", ""),
            max_chars=_MAX_CONTENT_CHARS,
        )
        for data_key in ("input_data", "output_data"):
            data_value = values.get(data_key)
            if not isinstance(data_value, dict):
                data_value = {}
            values[data_key] = redact_mapping(data_value)
        metadata = values.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {"value": metadata}
        values["metadata"] = redact_mapping(metadata)
        defaults = cls(
            event_id=str(values["event_id"]),
            run_id=str(values["run_id"]),
        )
        data = asdict(defaults)
        for key in data:
            if key in values and values[key] is not None:
                data[key] = values[key]
        return cls(**data)


def compact_content(payload: dict[str, Any]) -> str:
    return sanitize_text_fragment(payload, max_chars=_MAX_CONTENT_CHARS)


def dumps_event(event: CanonicalSessionEvent) -> str:
    return json.dumps(event.to_record(), ensure_ascii=False, sort_keys=True, default=str)
