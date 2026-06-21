"""Configuration helpers for the native ContextEngine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextStoreConfig:
    max_entries: int = 1000
    ttl_seconds: int | None = None


@dataclass(frozen=True)
class ContextSafetyConfig:
    skip_roles: tuple[str, ...] = ("user", "system")
    skip_tools: tuple[str, ...] = (
        "edit_file",
        "write_file",
        "write_markdown_file",
        "write_markdown_file_raw",
        "write_whole_file",
        "delete_file",
    )
    preserve_recent_errors: int = 1


@dataclass(frozen=True)
class ContextEngineConfig:
    min_chars: int = 2000
    preview_max_chars: int = 3000
    store: ContextStoreConfig = field(default_factory=ContextStoreConfig)
    safety: ContextSafetyConfig = field(default_factory=ContextSafetyConfig)

    @classmethod
    def from_mapping(cls, raw: Any) -> "ContextEngineConfig":
        if not isinstance(raw, dict):
            return cls()

        store_raw = raw.get("store", {})
        store = ContextStoreConfig(
            max_entries=_coerce_int(_mapping_get(store_raw, "max_entries"), 1000),
            ttl_seconds=_coerce_optional_int(_mapping_get(store_raw, "ttl_seconds")),
        )

        safety_raw = raw.get("safety", {})
        safety = ContextSafetyConfig(
            skip_roles=_coerce_tuple(_mapping_get(safety_raw, "skip_roles"), ("user", "system")),
            skip_tools=_coerce_tuple(
                _mapping_get(safety_raw, "skip_tools"),
                ContextSafetyConfig().skip_tools,
            ),
            preserve_recent_errors=_coerce_int(
                _mapping_get(safety_raw, "preserve_recent_errors"), 1
            ),
        )

        return cls(
            min_chars=_coerce_int(raw.get("min_chars"), 2000),
            preview_max_chars=_coerce_int(raw.get("preview_max_chars"), 3000),
            store=store,
            safety=safety,
        )

    @classmethod
    def from_runtime(cls) -> "ContextEngineConfig":
        from src.lib.config import C

        raw = C.get("context_engine", {})
        return cls.from_mapping(raw)


def _mapping_get(raw: Any, key: str) -> Any:
    return raw.get(key) if isinstance(raw, dict) else None


def _coerce_int(value: Any, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, result)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, result)


def _coerce_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        items = tuple(str(item) for item in value if str(item).strip())
        return items or default
    return default
