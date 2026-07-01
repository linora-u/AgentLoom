"""Request header policy for outbound model API calls."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from src.lib.config import C
from src.lib.config.model_request_header_profiles import (
    MODEL_REQUEST_HEADER_PROFILE_NAMES,
    MODEL_REQUEST_HEADER_PROFILES,
)

GENERIC_MODEL_USER_AGENT = "ai-agent/1.0"
AGENTLOOM_SESSION_UUID_TOKEN = "${agentloom.session_uuid}"
AGENTLOOM_SESSION_TOKEN_TOKEN = "${agentloom.session_token}"
_AGENTLOOM_SESSION_UUID = os.environ.get("AGENTLOOM_SESSION_UUID") or str(uuid4())
_AGENTLOOM_SESSION_TOKEN = os.environ.get("AGENTLOOM_SESSION_TOKEN") or (
    "ses_" + _AGENTLOOM_SESSION_UUID.replace("-", "")[:24]
)
ALLOWED_MODEL_REQUEST_HEADER_PROFILES = frozenset(
    {"agentloom", "generic", "none"}
    | MODEL_REQUEST_HEADER_PROFILE_NAMES
)


def _validate_header_part(value: str, *, field_name: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field_name} cannot contain CR/LF characters")


def _render_header_value(value: str) -> str:
    return (
        value.replace(AGENTLOOM_SESSION_UUID_TOKEN, _AGENTLOOM_SESSION_UUID)
        .replace(AGENTLOOM_SESSION_TOKEN_TOKEN, _AGENTLOOM_SESSION_TOKEN)
    )


def normalize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Return HTTP headers as a clean string-to-string mapping."""

    if not headers:
        return {}

    normalized: dict[str, str] = {}
    for raw_key, raw_value in headers.items():
        if raw_value is None:
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        if ":" in key:
            raise ValueError(f"HTTP header name cannot contain ':': {key!r}")
        _validate_header_part(key, field_name="HTTP header name")

        value = _render_header_value(str(raw_value))
        _validate_header_part(value, field_name=f"HTTP header value for {key!r}")
        normalized[key] = value
    return normalized


def merge_headers(*header_maps: Mapping[str, Any] | None) -> dict[str, str]:
    """Merge HTTP headers with case-insensitive override semantics."""

    merged: dict[str, str] = {}
    canonical_keys: dict[str, str] = {}
    for header_map in header_maps:
        for key, value in normalize_headers(header_map).items():
            lower_key = key.lower()
            previous_key = canonical_keys.get(lower_key)
            if previous_key is not None and previous_key != key:
                merged.pop(previous_key, None)
            merged[key] = value
            canonical_keys[lower_key] = key
    return merged


def _normalize_profile_name(value: Any) -> str:
    return str(value or "agentloom").strip().lower()


def _configured_profiles(raw_profiles: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw_profiles, Mapping):
        return {}

    profiles: dict[str, Mapping[str, Any]] = {}
    for raw_name, raw_profile in raw_profiles.items():
        name = _normalize_profile_name(raw_name)
        if not name or not isinstance(raw_profile, Mapping):
            continue
        profiles[name] = raw_profile
    return profiles


def _headers_from_custom_profile(profile_config: Mapping[str, Any]) -> Mapping[str, Any]:
    raw_headers = profile_config.get("headers")
    if isinstance(raw_headers, Mapping):
        return raw_headers
    return profile_config


def get_system_model_request_headers(
    config_map: Mapping[str, Any] | None = None,
    *,
    user_agent: str | None = None,
) -> dict[str, str]:
    """Resolve system-level default headers for model API requests."""

    root = config_map if config_map is not None else C.raw
    raw_cfg = root.get("model_request_headers", {}) if isinstance(root, Mapping) else {}
    cfg = raw_cfg if isinstance(raw_cfg, Mapping) else {}

    profile = _normalize_profile_name(cfg.get("profile"))
    custom_profiles = _configured_profiles(cfg.get("profiles", {}))
    if profile not in ALLOWED_MODEL_REQUEST_HEADER_PROFILES and profile not in custom_profiles:
        allowed = ", ".join(
            sorted(ALLOWED_MODEL_REQUEST_HEADER_PROFILES | set(custom_profiles))
        )
        raise ValueError(
            "model_request_headers.profile must be built-in or configured under "
            f"model_request_headers.profiles: {allowed}"
        )

    if profile in custom_profiles:
        profile_headers = normalize_headers(
            _headers_from_custom_profile(custom_profiles[profile])
        )
    elif profile == "agentloom":
        profile_headers = {"User-Agent": user_agent or C.user_agent}
    elif profile == "generic":
        profile_headers = {"User-Agent": GENERIC_MODEL_USER_AGENT}
    elif profile == "none":
        profile_headers = {}
    else:
        profile_headers = MODEL_REQUEST_HEADER_PROFILES[profile]

    return merge_headers(profile_headers, cfg.get("headers", {}))


def build_model_request_headers(model_headers: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Merge system-level defaults with model-specific headers."""

    return merge_headers(get_system_model_request_headers(), model_headers)
