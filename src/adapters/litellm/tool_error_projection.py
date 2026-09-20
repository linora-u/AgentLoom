"""Preserve canonical Tool errors in LiteLLM provider projections."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any


def _serialized_tool_record_is_error(message: dict[str, Any]) -> bool:
    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") in {"error", "blocked"}
    )


def _patch_provider_projection(
    original: Callable[[Any], Any],
    *,
    provider: str,
) -> Callable[[Any], Any]:
    @wraps(original)
    def project(message: dict[str, Any]) -> dict[str, Any]:
        result = original(message)
        if not _serialized_tool_record_is_error(message):
            return result
        if provider == "anthropic":
            result["is_error"] = True
        elif provider == "bedrock":
            tool_result = result.get("toolResult")
            if isinstance(tool_result, dict):
                tool_result["status"] = "error"
        return result

    project._agentloom_tool_error_patched = True  # type: ignore[attr-defined]
    return project


def patch_litellm_tool_error_projection() -> None:
    """Patch Anthropic and Bedrock Tool-result projections idempotently."""

    from litellm.litellm_core_utils.prompt_templates import factory

    factory_any: Any = factory
    anthropic: Any = factory.convert_to_anthropic_tool_result
    if not getattr(anthropic, "_agentloom_tool_error_patched", False):
        factory_any.convert_to_anthropic_tool_result = _patch_provider_projection(
            anthropic,
            provider="anthropic",
        )

    bedrock: Any = factory._convert_to_bedrock_tool_call_result
    if not getattr(bedrock, "_agentloom_tool_error_patched", False):
        factory_any._convert_to_bedrock_tool_call_result = _patch_provider_projection(
            bedrock,
            provider="bedrock",
        )
