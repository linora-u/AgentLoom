"""Resolve LiteLLM model profiles into canonical model-turn bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any

from agentloom.adapters.litellm.model_turn import create_model_turn_adapter
from agentloom.adapters.smolagents.models.request_headers import (
    build_model_request_headers,
)
from agentloom.configuration import C
from agentloom.configuration.llm_config import LlmModelTypeSettings
from agentloom.runtime.concurrency.rate_limiter import GlobalRateLimiterRegistry
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import ModelTurnAdapter

AdapterFactory = Callable[..., ModelTurnAdapter]

_BINDING_CACHE: dict[str, ModelTurnBinding] = {}
_BINDING_CACHE_LOCK = RLock()
_GOVERNANCE_LOCK = RLock()
_GOVERNANCE_INSTALLED = False


def _install_existing_model_governance() -> None:
    """Install the existing LiteLLM retry and Tool-result governance once."""

    global _GOVERNANCE_INSTALLED
    if _GOVERNANCE_INSTALLED:
        return
    with _GOVERNANCE_LOCK:
        if _GOVERNANCE_INSTALLED:
            return
        import litellm
        from agentloom.adapters.smolagents.models.litellm_retry import (
            patch_litellm_completion,
        )
        from agentloom.adapters.smolagents.tool_protocol import (
            patch_litellm_tool_error_projection,
        )

        litellm.suppress_debug_info = True
        litellm.drop_params = True
        patch_litellm_completion(litellm)
        patch_litellm_tool_error_projection()
        _GOVERNANCE_INSTALLED = True


def _normalized_model_type(model_type: str) -> str:
    normalized = str(model_type or "").strip().lower()
    if not normalized:
        raise ValueError("resolved model_type must be non-empty")
    return normalized


def _turn_options(
    model_type: str,
    settings: LlmModelTypeSettings,
    request_headers: Mapping[str, str],
) -> dict[str, Any]:
    output_token_key = (
        "max_output_tokens"
        if settings.adapter == "openai_responses"
        else "max_tokens"
    )
    options: dict[str, Any] = {
        output_token_key: settings.max_output_tokens,
        "temperature": settings.temperature,
        "timeout": settings.timeout,
        "num_retries": settings.num_retries,
        "retry_delay": settings.retry_delay,
        "max_retry_delay": settings.max_retry_delay,
        "_agent_loom_model_type": model_type,
    }
    if settings.base_url:
        options["api_base"] = settings.base_url
    if settings.api_key:
        options["api_key"] = settings.api_key
    if request_headers:
        options["extra_headers"] = dict(request_headers)
    if settings.extra_completion_params:
        options.update(settings.extra_completion_params)
    return options


def _build_binding(
    model_type: str,
    settings: LlmModelTypeSettings,
    *,
    request_headers: Mapping[str, str],
    adapter_factory: AdapterFactory,
) -> ModelTurnBinding:
    normalized_type = _normalized_model_type(model_type)
    _install_existing_model_governance()
    requests_per_minute = settings.requests_per_minute or 60
    GlobalRateLimiterRegistry.get_limiter(
        normalized_type,
        rpm=requests_per_minute,
    )
    return ModelTurnBinding(
        model_type=normalized_type,
        model_id=settings.model,
        adapter=adapter_factory(
            settings.adapter,
            context_cache=settings.context_cache,
            system_prompt_boundary=settings.system_prompt_boundary,
        ),
        options=_turn_options(normalized_type, settings, request_headers),
        max_tokens=settings.max_tokens,
        context_window=settings.context_window,
        max_output_tokens=settings.max_output_tokens,
        input_token_limit=settings.input_token_limit,
        requests_per_minute=requests_per_minute,
        description=settings.description,
    )


def build_litellm_model_turn_binding(
    model_type: str,
    settings: LlmModelTypeSettings,
    *,
    adapter_factory: AdapterFactory = create_model_turn_adapter,
) -> ModelTurnBinding:
    """Build a binding from one already parsed and validated model profile."""

    return _build_binding(
        model_type,
        settings,
        request_headers=build_model_request_headers(settings.extra_headers),
        adapter_factory=adapter_factory,
    )


def _cache_key(
    model_type: str,
    settings: LlmModelTypeSettings,
    request_headers: Mapping[str, str],
    adapter_factory: AdapterFactory,
) -> str:
    payload = {
        "model_type": model_type,
        "settings": settings.model_dump(mode="json"),
        "request_headers": dict(request_headers),
        "adapter_factory": id(adapter_factory),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve_litellm_model_turn_binding(
    model_type: str | None,
    *,
    model_cache: bool = True,
    adapter_factory: AdapterFactory = create_model_turn_adapter,
) -> ModelTurnBinding:
    """Resolve a configured model type without inferring or falling back protocols."""

    settings = C.llm.for_type(model_type)
    resolved_type = _normalized_model_type(
        model_type if model_type else C.llm.default_model_type
    )
    request_headers = build_model_request_headers(settings.extra_headers)
    if not model_cache:
        return _build_binding(
            resolved_type,
            settings,
            request_headers=request_headers,
            adapter_factory=adapter_factory,
        )

    cache_key = _cache_key(
        resolved_type,
        settings,
        request_headers,
        adapter_factory,
    )
    with _BINDING_CACHE_LOCK:
        cached = _BINDING_CACHE.get(cache_key)
        if cached is not None:
            return cached
        binding = _build_binding(
            resolved_type,
            settings,
            request_headers=request_headers,
            adapter_factory=adapter_factory,
        )
        _BINDING_CACHE[cache_key] = binding
        return binding


def clear_litellm_model_turn_binding_cache() -> None:
    """Clear resolved binding objects without changing provider configuration."""

    with _BINDING_CACHE_LOCK:
        _BINDING_CACHE.clear()
