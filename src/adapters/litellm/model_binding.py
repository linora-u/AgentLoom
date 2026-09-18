"""Resolve LiteLLM model profiles into canonical model-turn bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any

from agentloom.adapters.litellm.litellm_retry import patch_litellm_completion
from agentloom.adapters.litellm.model_turn import create_model_turn_adapter
from agentloom.adapters.litellm.request_headers import (
    build_model_request_headers,
)
from agentloom.adapters.litellm.tool_error_projection import (
    patch_litellm_tool_error_projection,
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


@dataclass(frozen=True, slots=True)
class ModelProfileOverlay:
    """Typed overrides applied after a configured model profile is resolved."""

    model_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    timeout: int | None = None
    num_retries: int | None = None
    retry_delay: float | None = None
    max_retry_delay: float | None = None
    extra_headers: dict[str, Any] | None = None
    context_cache: bool | None = None
    system_prompt_boundary: str | None = None
    description: str | None = None
    requests_per_minute: int | None = None
    extra_completion_params: dict[str, Any] | None = None

    def apply(
        self,
        settings: LlmModelTypeSettings,
    ) -> LlmModelTypeSettings:
        updates = {
            field_name: value
            for field_name, value in (
                ("model", self.model_id),
                ("base_url", self.base_url),
                ("api_key", self.api_key),
                ("temperature", self.temperature),
                ("max_tokens", self.max_tokens),
                ("context_window", self.context_window),
                ("max_output_tokens", self.max_output_tokens),
                ("timeout", self.timeout),
                ("num_retries", self.num_retries),
                ("retry_delay", self.retry_delay),
                ("max_retry_delay", self.max_retry_delay),
                ("extra_headers", self.extra_headers),
                ("context_cache", self.context_cache),
                ("system_prompt_boundary", self.system_prompt_boundary),
                ("description", self.description),
                ("requests_per_minute", self.requests_per_minute),
                ("extra_completion_params", self.extra_completion_params),
            )
            if value is not None
        }
        if self.max_tokens is not None and self.max_output_tokens is None:
            updates["max_output_tokens"] = self.max_tokens
        if {"context_window", "max_output_tokens"}.intersection(updates):
            context_window = int(
                updates.get("context_window", settings.context_window)
            )
            max_output_tokens = int(
                updates.get(
                    "max_output_tokens",
                    settings.max_output_tokens,
                )
            )
            if max_output_tokens >= context_window:
                raise ValueError(
                    "max_output_tokens must be smaller than context_window"
                )
            updates["input_token_limit"] = (
                context_window - max_output_tokens
            )
        return LlmModelTypeSettings.model_validate(
            settings.model_dump() | updates
        )


def _install_existing_model_governance() -> None:
    """Install the existing LiteLLM retry and Tool-result governance once."""

    global _GOVERNANCE_INSTALLED
    if _GOVERNANCE_INSTALLED:
        return
    with _GOVERNANCE_LOCK:
        if _GOVERNANCE_INSTALLED:
            return
        import litellm
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
    profile_overlay: ModelProfileOverlay | None = None,
    model_cache: bool = True,
    adapter_factory: AdapterFactory = create_model_turn_adapter,
) -> ModelTurnBinding:
    """Resolve a configured model type without inferring or falling back protocols."""

    settings = C.llm.for_type(model_type)
    if profile_overlay is not None:
        settings = profile_overlay.apply(settings)
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
