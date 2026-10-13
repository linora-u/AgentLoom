"""Resolve LiteLLM model profiles into canonical model-turn bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any

from agentloom.config import C
from agentloom.config.http_headers import normalize_http_headers
from agentloom.config.llm_config import LLMConfig, LlmModelTypeSettings
from agentloom.config.output_budget import UNSET_BUDGET, UnsetBudget
from agentloom.execution.concurrency.rate_limiter import GlobalRateLimiterRegistry
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import ModelTurnAdapter
from agentloom.integrations.litellm.litellm_retry import patch_litellm_completion
from agentloom.integrations.litellm.model_turn import create_model_turn_adapter
from agentloom.integrations.litellm.tool_error_projection import (
    patch_litellm_tool_error_projection,
)

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
    max_output_tokens: int | None | UnsetBudget = UNSET_BUDGET
    input_token_reserve: int | None = None
    max_model_output_tokens: int | None = None
    model_output_capability_source: str | None = None
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
                ("input_token_reserve", self.input_token_reserve),
                ("max_model_output_tokens", self.max_model_output_tokens),
                ("model_output_capability_source", self.model_output_capability_source),
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
            if value is not UNSET_BUDGET and (value is not None or field_name == "max_output_tokens")
        }
        if self.max_tokens is not None and self.max_output_tokens is UNSET_BUDGET:
            updates["max_output_tokens"] = self.max_tokens
        merged = settings.model_dump() | updates
        if {"context_window", "max_output_tokens", "input_token_reserve"}.intersection(updates):
            merged.pop("input_token_limit", None)
        if "input_token_reserve" in updates:
            merged.pop("input_reserve_source", None)
        if "max_output_tokens" in updates:
            merged.pop("output_cap_source", None)
            if settings.output_cap_source == "legacy_max_tokens" and "input_token_reserve" not in updates:
                # Legacy input allowance equals the whole window; it is not an
                # independent reserve that can be retained after migration.
                merged.pop("input_token_reserve", None)
                merged.pop("input_reserve_source", None)
        return LlmModelTypeSettings.model_validate(merged)



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
    rate_limit_key: str,
) -> dict[str, Any]:
    output_token_key = (
        "max_output_tokens"
        if settings.adapter == "openai_responses"
        else "max_tokens"
    )
    options: dict[str, Any] = {
        "temperature": settings.temperature,
        "timeout": settings.timeout,
        "num_retries": settings.num_retries,
        "retry_delay": settings.retry_delay,
        "max_retry_delay": settings.max_retry_delay,
        "_agent_loom_model_type": model_type,
        "_agent_loom_rate_limit_key": rate_limit_key,
    }
    if settings.max_output_tokens is not None:
        options[output_token_key] = settings.max_output_tokens
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
    # Same profile names in different Applications may address different
    # providers or have different RPM limits. Match the actual model settings.
    encoded = json.dumps(settings.model_dump(mode="json"), sort_keys=True).encode()
    rate_limit_key = f"{normalized_type}:{hashlib.sha256(encoded).hexdigest()}"
    GlobalRateLimiterRegistry.get_limiter(
        rate_limit_key,
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
        options=_turn_options(normalized_type, settings, request_headers, rate_limit_key),
        max_tokens=settings.max_tokens,
        context_window=settings.context_window,
        max_output_tokens=settings.max_output_tokens,
        output_cap_source=settings.output_cap_source,
        input_token_reserve=settings.input_token_reserve,
        input_reserve_source=settings.input_reserve_source,
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
        request_headers=normalize_http_headers(settings.extra_headers),
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
    llm_config: LLMConfig | None = None,
    profile_overlay: ModelProfileOverlay | None = None,
    model_cache: bool = True,
    adapter_factory: AdapterFactory = create_model_turn_adapter,
) -> ModelTurnBinding:
    """Resolve a configured model type without inferring or falling back protocols."""

    catalog = llm_config if llm_config is not None else C.llm
    settings = catalog.for_type(model_type)
    if profile_overlay is not None:
        settings = profile_overlay.apply(settings)
    resolved_type = _normalized_model_type(
        model_type if model_type else catalog.default_model_type
    )
    request_headers = normalize_http_headers(settings.extra_headers)
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
