"""
Model type definitions and configuration.
"""

import json
from dataclasses import dataclass
from typing import Optional

from src.lib.config import C
from src.lib.config.defaults import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_CONTEXT_CACHE,
    DEFAULT_MODEL_MAX_RETRY_DELAY,
    DEFAULT_MODEL_NUM_RETRIES,
    DEFAULT_MODEL_REQUESTS_PER_MINUTE,
    DEFAULT_MODEL_RETRY_DELAY,
    DEFAULT_MODEL_TEMPERATURE,
    DEFAULT_MODEL_TIMEOUT,
)
from src.lib.logging import get_logger

logger = get_logger(__name__)


def _available_types_text(available: list[str]) -> str:
    return ", ".join(available) if available else "(none)"


def _missing_default_model_type_error(available: list[str]) -> str:
    return (
        "No model_type was provided and config/llm.yaml does not set "
        "`model.default_model_type`; the model call was not started. "
        "Fix: add `model_type: <type>` to the Agent YAML, or set "
        "`model.default_model_type: <type>` in config/llm.yaml. "
        f"Available model types: {_available_types_text(available)}."
    )


def _unknown_model_type_error(model_type: str, available: list[str]) -> str:
    return (
        f"Model type '{model_type}' is not defined in config/llm.yaml; "
        "the model call was not started. "
        f"Available model types: {_available_types_text(available)}. "
        "Fix: set Agent YAML `model_type` to one of the available types, "
        f"or add `model.{model_type}.model: <provider/model>` in config/llm.yaml."
    )


@dataclass
class ModelConfig:
    """Model configuration."""
    model_id: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = DEFAULT_MODEL_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    context_window: int = DEFAULT_MAX_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    input_token_limit: int = DEFAULT_MAX_TOKENS - DEFAULT_MAX_OUTPUT_TOKENS
    timeout: int = DEFAULT_MODEL_TIMEOUT  # seconds
    description: str = ""
    num_retries: int = DEFAULT_MODEL_NUM_RETRIES
    retry_delay: float = DEFAULT_MODEL_RETRY_DELAY
    max_retry_delay: float = DEFAULT_MODEL_MAX_RETRY_DELAY
    extra_headers: Optional[dict] = None
    context_cache: bool = DEFAULT_MODEL_CONTEXT_CACHE
    system_prompt_boundary: Optional[str] = None
    requests_per_minute: int = DEFAULT_MODEL_REQUESTS_PER_MINUTE
    # Whether the model supports json_schema structured output (response_format).
    # "true" - use structured output (json_schema) for code_act mode
    # "false" - use text-based <code> block parsing for code_act mode
    supports_structured_output: str = "false"
    # Extra parameters passed through to litellm.completion() (e.g. reasoning_effort,
    # extra_body for provider-specific features like DeepSeek thinking mode).
    extra_completion_params: Optional[dict] = None

    @property
    def usable_input_tokens(self) -> int:
        return self.input_token_limit


class ModelType:
    """
    Dynamic model type.
    """

    def __init__(self, type_name: str):
        self._name = type_name.lower().strip()

    @property
    def value(self) -> str:
        return self._name

    @property
    def name(self) -> str:
        return self._name.upper()

    def __eq__(self, other) -> bool:
        if isinstance(other, ModelType):
            return self._name == other._name
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"ModelType({self._name!r})"


    @classmethod
    def _make_const(cls, name: str) -> "ModelType":
        return cls(name)


ModelType.POWERFUL = ModelType._make_const("powerful")
ModelType.FAST = ModelType._make_const("fast")
ModelType.SUMMARY = ModelType._make_const("summary")
ModelType.CUSTOM = ModelType._make_const("custom")


def _build_model_config_from_yaml(type_name: str) -> ModelConfig:
    """
    Build a ModelConfig by reading merged config model section.
    """
    resolved = C.llm.for_type(type_name)

    # Parse extra_headers: accept dict or JSON string.
    raw_headers = resolved.extra_headers
    if isinstance(raw_headers, dict):
        extra_headers = raw_headers
    elif isinstance(raw_headers, str) and raw_headers.strip():
        try:
            parsed = json.loads(raw_headers)
            extra_headers = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, ValueError):
            extra_headers = None
    else:
        extra_headers = None

    return ModelConfig(
        model_id=resolved.model,
        base_url=resolved.base_url or None,
        api_key=resolved.api_key or None,
        temperature=float(resolved.temperature),
        max_tokens=int(resolved.max_tokens),
        context_window=int(resolved.context_window),
        max_output_tokens=int(resolved.max_output_tokens),
        input_token_limit=int(resolved.input_token_limit),
        timeout=int(resolved.timeout),
        description=resolved.description,
        num_retries=int(resolved.num_retries),
        retry_delay=float(resolved.retry_delay),
        max_retry_delay=float(resolved.max_retry_delay),
        extra_headers=extra_headers,
        context_cache=bool(resolved.context_cache),
        system_prompt_boundary=getattr(resolved, 'system_prompt_boundary', None),
        requests_per_minute=int(resolved.requests_per_minute),
        supports_structured_output=getattr(resolved, 'supports_structured_output', 'false'),
        extra_completion_params=getattr(resolved, 'extra_completion_params', None),
    )

class ModelTypeManager:
    """Model type manager – fully YAML-driven."""

    @classmethod
    def _get_available_types(cls) -> list[str]:
        """
        Return all model type names defined in config under 'model:',
        excluding reserved keys like 'default_model_type'.
        """
        return list(C.llm.available_types)

    @classmethod
    def get_llm_config(cls, model_type: "ModelType") -> ModelConfig:
        """
        Get LLM config for a model type.

        Args:
            model_type: ModelType instance.

        Returns:
            ModelConfig: LLM config for the model type.

        Raises:
            ValueError: Raised when the model type is not defined in YAML.
        """
        available = cls._get_available_types()
        if model_type.value not in available:
            raise ValueError(_unknown_model_type_error(model_type.value, available))
        return _build_model_config_from_yaml(model_type.value)

    @classmethod
    def get_description(cls, model_type: "ModelType") -> str:
        """
        Get model-type description.

        Args:
            model_type: ModelType instance.

        Returns:
            str: Model description.
        """
        return cls.get_llm_config(model_type).description

    @classmethod
    def resolve_model_type(cls, model_type: Optional[str]) -> "ModelType":
        """
        Resolve a model type string to a ModelType instance.

        If `model_type` is None or empty, uses the global default model type
        from YAML (model.default_model_type). If that key is unset, the
        framework raises ``ValueError`` immediately.

        If `model_type` is explicitly specified but not defined in
        llm.yaml, a ``ValueError`` is raised to surface configuration
        errors early.

        Args:
            model_type: Model type string (can be None),
                      e.g. "powerful", "fast", "my_custom_type"

        Returns:
            ModelType: Resolved model type.

        Raises:
            ValueError: When the explicitly requested model type is not
                defined in llm.yaml.
        """
        if not model_type:
            default_type = (C.default_model_type or "").strip()
            if not default_type:
                raise ValueError(_missing_default_model_type_error(cls._get_available_types()))
            return ModelType(default_type)

        type_name = model_type.lower().strip()
        available = cls._get_available_types()

        if type_name in available:
            return ModelType(type_name)

        raise ValueError(_unknown_model_type_error(model_type, available))
