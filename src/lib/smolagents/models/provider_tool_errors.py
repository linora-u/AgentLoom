"""Provider-native projection for canonical AgentLoom tool failures."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any


def _tool_message_is_error(message: dict[str, Any]) -> bool:
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
    return isinstance(payload, dict) and (
        payload.get("ok") is False
        or payload.get("status") in {"error", "blocked", "cancelled"}
    )


def _patch_projection(
    original: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    provider: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    @wraps(original)
    def project(message: dict[str, Any]) -> dict[str, Any]:
        result = original(message)
        if not _tool_message_is_error(message):
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
    """Add native Anthropic/Bedrock error flags to LiteLLM projections.

    LiteLLM receives OpenAI-shaped tool messages, which cannot express a tool
    error. It therefore defaults provider projections to success. AgentLoom's
    canonical JSON envelope restores that missing signal. These pure wrappers
    use no request-local mutable state, so cached models remain thread-safe.
    """
    from litellm.litellm_core_utils.prompt_templates import factory

    anthropic = factory.convert_to_anthropic_tool_result
    if not getattr(anthropic, "_agentloom_tool_error_patched", False):
        factory.convert_to_anthropic_tool_result = _patch_projection(
            anthropic,
            provider="anthropic",
        )

    bedrock = factory._convert_to_bedrock_tool_call_result
    if not getattr(bedrock, "_agentloom_tool_error_patched", False):
        factory._convert_to_bedrock_tool_call_result = _patch_projection(
            bedrock,
            provider="bedrock",
        )
