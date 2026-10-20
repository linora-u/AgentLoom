"""Reject bound credentials before generated provider content reaches storage."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agentloom.config.private_bindings import harness_private_values

from .model_protocol import ModelResponseRejected, ModelUsage


class BoundCredentialEcho(RuntimeError):
    """An executed tool returned private data; it must not be retried."""

    kind = "credential_echo"
    stage = "output_validation"
    retryable = False


def contains_private_content(value: Any) -> bool:
    values = harness_private_values()
    if values is None:
        return False
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        value = dump()

    def contains(item: Any) -> bool:
        if isinstance(item, str):
            if any(secret in item for secret in values):
                return True
            # Tool arguments and structured final answers are JSON strings on
            # the wire. Check their decoded values before any native parser.
            if item.lstrip().startswith(("{", "[")):
                try:
                    return contains(json.loads(item))
                except (ValueError, RecursionError):
                    pass
            return False
        if isinstance(item, Mapping):
            return any(contains(key) or contains(part) for key, part in item.items())
        if isinstance(item, (tuple, list)):
            return any(contains(part) for part in item)
        return False
    return contains(value)


def reject_private_content(value: Any) -> None:
    if contains_private_content(value):
        raise BoundCredentialEcho("Tool response contained a bound credential")


def reject_private_response(response: Any, *, model: str | None = None) -> None:
    if harness_private_values() is None:
        return
    dump = getattr(response, "model_dump", None)
    data = dump() if callable(dump) else response
    public_models = {model} if model else set()
    if model and model.startswith(("openai/", "anthropic/", "gemini/")):
        public_models.add(model.split("/", 1)[1])
    enums = {
        "type": {"message", "text", "output_text", "input_text", "function", "function_call",
                 "tool_use", "thinking", "redacted_thinking", "reasoning", "summary_text"},
        "role": {"assistant"}, "status": {"completed", "in_progress", "incomplete"},
        "object": {"chat.completion", "chat.completion.chunk", "response"},
        "finish_reason": {"stop", "tool_calls", "function_call", "length", "content_filter"},
        "stop_reason": {"end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"},
    }
    fields = {"id", "type", "role", "model", "object", "created", "created_at", "status", "usage",
              "choices", "output", "content", "text", "message", "delta", "index", "finish_reason",
              "stop_reason", "stop_sequence", "tool_calls", "function", "name", "arguments", "input",
              "reasoning", "reasoning_content", "refusal", "summary", "annotations", "signature",
              "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens",
              "prompt_tokens_details", "completion_tokens_details", "cached_tokens", "reasoning_tokens",
              "service_tier", "system_fingerprint", "error", "errorMessage", "error_message"}

    def wire(value):
        if isinstance(value, list):
            return [wire(part) for part in value]
        if not isinstance(value, Mapping):
            return value
        result = []
        for key, part in value.items():
            if key not in fields:
                result.append(key)
            if key == "model" and isinstance(part, str) and part in public_models:
                continue
            if key in enums and isinstance(part, str) and part in enums[key]:
                continue
            # Generated argument objects retain every arbitrary nested key.
            if key in {"arguments", "input"} or (key == "content" and isinstance(part, Mapping)):
                result.append(part)
            else:
                result.append(wire(part))
        return result

    generated = wire(data)
    if not contains_private_content(generated):
        return
    raw = data.get("usage", {}) if isinstance(data, Mapping) else {}
    safe = {key: value for key, value in raw.items() if key in {
        "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens",
    } and type(value) is int and value >= 0} if isinstance(raw, Mapping) else {}
    raise ModelResponseRejected(ModelUsage(
        input_tokens=safe.get("prompt_tokens", safe.get("input_tokens", 0)),
        output_tokens=safe.get("completion_tokens", safe.get("output_tokens", 0)),
        total_tokens=safe.get("total_tokens", 0), details=safe,
    ))


def contains_private_error(error: Exception) -> bool:
    visited = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if contains_private_content(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


def safe_provider_error(error: Exception) -> Exception:
    """Return an exception with no secret response or unsafe exception chain."""
    if isinstance(error, ModelResponseRejected):
        return error
    if contains_private_error(error):
        return ModelResponseRejected(ModelUsage())
    return error


def rejection_usage(error: Exception) -> dict[str, Any] | None:
    return {"usage": error.usage.details} if isinstance(error, ModelResponseRejected) else None
