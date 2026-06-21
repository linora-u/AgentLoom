"""Data models for reversible context compression."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class ContentKind(str, Enum):
    JSON = "json"
    SEARCH = "search"
    LOG = "log"
    DIFF = "diff"
    CODE = "code"
    TEXT = "text"


@dataclass(frozen=True)
class CompressionResult:
    kind: ContentKind
    preview: str
    original_chars: int
    preview_chars: int
    original_tokens_est: int
    preview_tokens_est: int
    strategy: str

    @property
    def chars_saved(self) -> int:
        return max(0, self.original_chars - self.preview_chars)


@dataclass(frozen=True)
class ContextEntry:
    ref: str
    kind: ContentKind
    tool_name: str
    original: str
    preview: str
    original_chars: int
    preview_chars: int
    original_tokens_est: int
    preview_tokens_est: int
    strategy: str
    created_at: float
    ttl_seconds: int | None = None
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ContextEntry":
        return cls(
            ref=str(data["ref"]),
            kind=ContentKind(str(data.get("kind", ContentKind.TEXT.value))),
            tool_name=str(data.get("tool_name", "default")),
            original=str(data.get("original", "")),
            preview=str(data.get("preview", "")),
            original_chars=int(data.get("original_chars", 0)),
            preview_chars=int(data.get("preview_chars", 0)),
            original_tokens_est=int(data.get("original_tokens_est", 0)),
            preview_tokens_est=int(data.get("preview_tokens_est", 0)),
            strategy=str(data.get("strategy", "")),
            created_at=float(data.get("created_at", 0.0)),
            ttl_seconds=data.get("ttl_seconds"),
            source=str(data.get("source", "")),
        )
