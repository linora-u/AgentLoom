"""Compatibility re-exports for the LiteLLM-owned request-header policy."""

from agentloom.adapters.litellm.request_headers import (
    AGENTLOOM_SESSION_TOKEN_TOKEN,
    AGENTLOOM_SESSION_UUID_TOKEN,
    ALLOWED_MODEL_REQUEST_HEADER_PROFILES,
    GENERIC_MODEL_USER_AGENT,
    build_model_request_headers,
    get_system_model_request_headers,
    merge_headers,
    normalize_headers,
)

__all__ = [
    "AGENTLOOM_SESSION_TOKEN_TOKEN",
    "AGENTLOOM_SESSION_UUID_TOKEN",
    "ALLOWED_MODEL_REQUEST_HEADER_PROFILES",
    "GENERIC_MODEL_USER_AGENT",
    "build_model_request_headers",
    "get_system_model_request_headers",
    "merge_headers",
    "normalize_headers",
]
