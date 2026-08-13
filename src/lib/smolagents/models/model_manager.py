"""
Model manager.

Provides unified management of different model types, including model selection
and configuration.
"""

import json
from dataclasses import dataclass, replace

import litellm

from smolagents import AgentLogger
from src.lib.logging import get_logger
from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
from src.lib.smolagents.models.litellm_retry import patch_litellm_completion
from src.lib.smolagents.models.request_headers import (
    build_model_request_headers,
    get_system_model_request_headers,
)
from src.lib.smolagents.tool_protocol import patch_litellm_tool_error_projection

from .model_types import ModelConfig, ModelType, ModelTypeManager

litellm.suppress_debug_info = True
litellm.drop_params = True

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModelConfigOverlay:
    model_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    timeout: int | None = None
    description: str | None = None
    num_retries: int | None = None
    retry_delay: float | None = None
    max_retry_delay: float | None = None
    extra_headers: dict | None = None
    context_cache: bool | None = None
    system_prompt_boundary: str | None = None
    requests_per_minute: int | None = None
    extra_completion_params: dict | None = None

    def to_mapping(self) -> dict:
        mapping = {k: v for k, v in self.__dict__.items() if v is not None}
        if self.max_tokens is not None and self.max_output_tokens is None:
            mapping["max_output_tokens"] = self.max_tokens
        return mapping


class ModelConfigBuilder:
    """Build model config with ordered typed overlays."""

    def __init__(self) -> None:
        self._layers: list[tuple[str, dict]] = []

    def apply_overlay(
        self,
        overlay: ModelConfigOverlay,
        *,
        source: str = "overlay",
    ) -> "ModelConfigBuilder":
        self._layers.append((source, overlay.to_mapping()))
        return self

    def build(self, base_config: ModelConfig) -> ModelConfig:
        merged = replace(base_config)
        split_budget_overridden = False
        for _source, layer in self._layers:
            if not layer:
                continue
            split_budget_overridden = split_budget_overridden or bool(
                {"context_window", "max_output_tokens"}.intersection(layer)
            )
            merged = replace(merged, **layer)
        if split_budget_overridden:
            if not isinstance(merged.max_output_tokens, int):
                raise ValueError("max_output_tokens overlay must be an integer")
            if merged.max_output_tokens >= merged.context_window:
                raise ValueError("max_output_tokens must be smaller than context_window")
            merged.input_token_limit = merged.context_window - merged.max_output_tokens
        return merged

    def cache_fragment(self) -> str:
        if not self._layers:
            return ""
        serializable = [{"source": source, "data": layer} for source, layer in self._layers]
        return json.dumps(serializable, sort_keys=True, default=str)


class ModelManager:
    """
    Model manager.

    Manages different model types and provides unified model retrieval APIs.
    """

    def __init__(self):
        """Initialize model manager."""
        self._model_cache = {}

        # Configure global litellm retry settings.
        self._configure_litellm_retry()

        logger.debug("Model manager initialized")

    def _configure_litellm_retry(self):
        """Configure litellm retry behavior."""

        # Apply custom retry wrapper.
        patch_litellm_completion(litellm)
        patch_litellm_tool_error_projection()

        # Configure global headers from config for litellm versions that expose
        # a module-level default. Per-call extra_headers is the authoritative path.
        system_headers = get_system_model_request_headers()
        if hasattr(litellm, 'default_headers'):
            litellm.default_headers = system_headers

        logger.debug(
            "Configured litellm retry: exponential backoff via tenacity, "
            "model request headers: %s",
            sorted(system_headers),
        )

    def get_model_config(
        self,
        model_type: ModelType,
        model_builder: ModelConfigBuilder | None = None
    ) -> ModelConfig:
        """
        Get model configuration.

        Args:
            model_type: Model type.
            model_builder: Overlay builder for runtime model overrides.

        Returns:
            ModelConfig: Full model configuration.
        """
        # Get base configuration.
        base_config = ModelTypeManager.get_llm_config(model_type)

        effective_builder = model_builder or ModelConfigBuilder()
        return effective_builder.build(base_config)

    def _generate_cache_key(
        self,
        prefix: str,
        model_type: ModelType,
        model_builder: ModelConfigBuilder | None = None,
    ) -> str:
        """Generate a deterministic cache key including builder overlays."""
        cache_key = f"{prefix}_{model_type.value}"
        if model_builder is not None:
            builder_fragment = model_builder.cache_fragment()
            if builder_fragment:
                cache_key += f"_{hash(builder_fragment)}"
        return cache_key

    def get_litellm_config(
        self,
        model_type: ModelType,
        model_builder: ModelConfigBuilder | None = None,
        model_cache: bool = True
    ) -> dict:
        """
        Get configuration parameters for `litellm.completion`.

        Args:
            model_type: Model type.
            model_builder: Overlay builder for runtime model overrides.
            model_cache: Whether to use object cache.

        Returns:
            dict: Configuration parameters for `litellm.completion`.
        """
        cache_key = self._generate_cache_key("litellm_config", model_type, model_builder)

        if model_cache and cache_key in self._model_cache:
            return self._model_cache[cache_key]

        model_config = self.get_model_config(model_type, model_builder)

        # Build parameters for litellm.completion.
        # If new parameters are added here, verify they are supported by litellm.
        # Unsupported parameters may be forwarded to provider APIs and cause failures.
        # Retry-related params (retry_delay, max_retry_delay) are removed in retry wrapper.
        litellm_params: dict[str, object] = {
            "model": model_config.model_id,
            "temperature": model_config.temperature,
            "max_tokens": model_config.max_output_tokens,
            # Retry-related parameters
            "num_retries": model_config.num_retries,
            "retry_delay": model_config.retry_delay,
            "max_retry_delay": model_config.max_retry_delay,
            "timeout": model_config.timeout,
        }

        # Add system-level privacy headers plus model-specific overrides.
        request_headers = build_model_request_headers(model_config.extra_headers)
        if request_headers:
            litellm_params["extra_headers"] = request_headers

        # Add API fields only if configured.
        if model_config.base_url:
            litellm_params["api_base"] = model_config.base_url
        if model_config.api_key:
            litellm_params["api_key"] = model_config.api_key

        # Merge extra completion params (reasoning_effort, extra_body, etc.)
        if model_config.extra_completion_params:
            litellm_params.update(model_config.extra_completion_params)

        if model_cache:
            self._model_cache[cache_key] = litellm_params

        logger.debug(f"Resolved litellm config: {model_type.value} -> {model_config.model_id}")
        return litellm_params

    def get_smolagents_model(
        self,
        model_type: ModelType,
        model_builder: ModelConfigBuilder | None = None,
        model_cache: bool = True,
        logger: AgentLogger | None = None
    ) -> LiteLLMModelV2:
        """
        Get a model instance for smolagents.

        Args:
            model_type: Model type.
            model_builder: Overlay builder for runtime model overrides.
            model_cache: Whether to use object cache.
            logger: Logger instance.

        Returns:
            LiteLLMModelV2: Smolagents model instance.
        """
        cache_key = self._generate_cache_key("smolagents", model_type, model_builder)

        if model_cache and cache_key in self._model_cache:
            return self._model_cache[cache_key]

        runtime_logger = get_logger(logger, __name__) if logger is not None else None
        model_config = self.get_model_config(model_type, model_builder)
        _log = runtime_logger or logger
        if _log:
            safe_model_config = model_config.__dict__.copy()
            safe_model_config["api_key"] = "***" if model_config.api_key else None
            _log.info(f"Creating smolagents model with config: {safe_model_config}")


        # Create smolagents model with retry settings.
        # If new parameters are added here, verify litellm/provider compatibility.
        # Retry params are removed in retry wrapper and will not be forwarded downstream.
        # Build optional kwargs that should only be passed when set.
        # extra_headers flows through smolagents self.kwargs → litellm.completion() natively.
        optional_kwargs = {}
        request_headers = build_model_request_headers(model_config.extra_headers)
        if request_headers:
            optional_kwargs["extra_headers"] = request_headers
        if model_config.extra_completion_params:
            optional_kwargs.update(model_config.extra_completion_params)

        model = LiteLLMModelV2(
            model_id=model_config.model_id,
            api_base=model_config.base_url,
            api_key=model_config.api_key,
            timeout=model_config.timeout,
            max_tokens=model_config.max_output_tokens,
            temperature=model_config.temperature,
            requests_per_minute=model_config.requests_per_minute or 10,
            # Retry-related parameters
            num_retries=model_config.num_retries,
            retry_delay=model_config.retry_delay,
            max_retry_delay=model_config.max_retry_delay,
            logger=runtime_logger,
            context_cache=model_config.context_cache,
            system_prompt_boundary=model_config.system_prompt_boundary,
            supports_structured_output=model_config.supports_structured_output,
            **optional_kwargs,
        )

        # Inject model_type for global rate limiting (consumed by litellm_retry wrapper)
        model._agent_loom_model_type = model_type.value

        # Pre-register rate limiter with the configured RPM so litellm_retry
        # doesn't fall back to the default (10 RPM).
        try:
            from src.lib.concurrency.rate_limiter import GlobalRateLimiterRegistry
            _rpm = model_config.requests_per_minute or 60
            GlobalRateLimiterRegistry.get_limiter(model_type.value, rpm=_rpm)
        except Exception:
            pass

        if model_cache:
            self._model_cache[cache_key] = model

        if runtime_logger:
            runtime_logger.info(f"Resolved smolagents model: {model_type.value} -> {model_config.model_id}")
        return model

    def get_model(
        self,
        model_type: str | None,
        framework: str = "litellm",
        model_builder: ModelConfigBuilder | None = None,
        model_cache: bool = True,
        logger: AgentLogger | None = None
    ) -> dict | LiteLLMModelV2:
        """
        Get an appropriate model based on model type.

        Args:
            model_type: Model type.
            framework: Framework to use ("litellm" or "smolagents").
            model_builder: Overlay builder for runtime model overrides.
            model_cache: Whether to use object cache.
            logger: Logger instance.

        Returns:
            Union[dict, LiteLLMModelV2]: LiteLLM config dict or smolagents model instance.
        """
        resolved_type = ModelTypeManager.resolve_model_type(model_type)

        runtime_logger = get_logger(logger, __name__) if logger is not None else None
        if runtime_logger:
            runtime_logger.info(f"Requested model type '{model_type}', resolved to: {resolved_type.value}")

        if framework.lower() == "litellm":
            return self.get_litellm_config(resolved_type, model_builder, model_cache)
        elif framework.lower() == "smolagents":
            return self.get_smolagents_model(resolved_type, model_builder, model_cache, logger=runtime_logger)
        else:
            raise ValueError(f"Unsupported framework: {framework}")

    def clear_cache(self):
        """Clear model cache."""
        self._model_cache.clear()
        logger.info("Model cache cleared")

    def get_cache_info(self) -> dict:
        """
        Get cache information.

        Returns:
            dict: Cache information.
        """
        return {
            "cached_models": list(self._model_cache.keys()),
            "cache_size": len(self._model_cache)
        }

# Global model manager instance.
model_manager = ModelManager()


def get_model(
    model_type: str | None,
    framework: str = "litellm",
    model_builder: ModelConfigBuilder | None = None,
    model_cache: bool = True,
    logger: AgentLogger | None = None
) -> dict | LiteLLMModelV2:
    """
    Convenience helper: get a model by model type.

    Args:
        model_type: Model type.
        framework: Framework to use.
        model_builder: Overlay builder for runtime model overrides.
        logger: Logger instance.

    Returns:
        Union[dict, LiteLLMModelV2]: LiteLLM config dict or smolagents model instance.
    """
    return model_manager.get_model(model_type, framework, model_builder, model_cache, logger=logger)
