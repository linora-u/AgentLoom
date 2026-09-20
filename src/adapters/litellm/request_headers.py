"""Compatibility exports for the shared model request-header policy."""
from agentloom.configuration.model_request_headers import (
    GENERIC_MODEL_USER_AGENT as GENERIC_MODEL_USER_AGENT,
    AGENTLOOM_SESSION_UUID_TOKEN as AGENTLOOM_SESSION_UUID_TOKEN,
    AGENTLOOM_SESSION_TOKEN_TOKEN as AGENTLOOM_SESSION_TOKEN_TOKEN,
    ALLOWED_MODEL_REQUEST_HEADER_PROFILES as ALLOWED_MODEL_REQUEST_HEADER_PROFILES,
    normalize_headers as normalize_headers,
    merge_headers as merge_headers,
    get_system_model_request_headers as get_system_model_request_headers,
    build_model_request_headers as build_model_request_headers,
)
