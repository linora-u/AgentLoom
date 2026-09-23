"""Normalize explicit per-model HTTP request headers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_http_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate and copy explicitly configured headers without adding defaults."""

    normalized: dict[str, str] = {}
    canonical_keys: dict[str, str] = {}
    for raw_key, raw_value in (headers or {}).items():
        if raw_value is None:
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        if ":" in key or "\r" in key or "\n" in key:
            raise ValueError(f"Invalid HTTP header name: {key!r}")
        value = str(raw_value)
        if "\r" in value or "\n" in value:
            raise ValueError(f"Invalid HTTP header value for {key!r}")
        previous_key = canonical_keys.get(key.lower())
        if previous_key is not None:
            normalized.pop(previous_key)
        normalized[key] = value
        canonical_keys[key.lower()] = key
    return normalized
