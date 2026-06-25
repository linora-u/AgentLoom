"""Canonical self-learning event schema."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .redaction import redact_mapping, redact_text

_MAX_CONTENT_CHARS = 60000


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def safe_run_id(value: str) -> str:
    raw = str(value or "").strip() or uuid.uuid4().hex
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    return cleaned[:120] or uuid.uuid4().hex


@dataclass(frozen=True)
class CanonicalSessionEvent:
    """Append-only record used as the self-learning source of truth."""

    event_id: str
    run_id: str
    schema_version: int = 2
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

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["content"] = redact_text(record.get("content", ""), max_chars=_MAX_CONTENT_CHARS)
        record["content_text"] = redact_text(
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
        values.setdefault("created_at", now_iso())
        values.setdefault("source", "hook")
        values.setdefault("schema_version", 2)
        values["content"] = redact_text(values.get("content", ""), max_chars=_MAX_CONTENT_CHARS)
        values["content_text"] = redact_text(
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
    return redact_text(payload, max_chars=_MAX_CONTENT_CHARS)


def dumps_event(event: CanonicalSessionEvent) -> str:
    return json.dumps(event.to_record(), ensure_ascii=False, sort_keys=True, default=str)
