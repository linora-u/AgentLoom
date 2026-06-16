"""
Independent Parsing for LLM Configuration (llm.yaml)
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pathlib import Path
import yaml

from src.lib.config.config_validation import BoolParser, IntParser
from src.lib.config.defaults import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_CONTEXT_CACHE,
    DEFAULT_MODEL_MAX_RETRY_DELAY,
    DEFAULT_MODEL_NUM_RETRIES,
    DEFAULT_MODEL_REQUESTS_PER_MINUTE,
    DEFAULT_MODEL_RETRY_DELAY,
    DEFAULT_MODEL_TEMPERATURE,
    DEFAULT_MODEL_TIMEOUT,
)

_RESERVED_MODEL_KEYS = {"default_model_type", "common"}


def _available_types_text(models: Dict[str, Any]) -> str:
    available = list(models.keys())
    return ", ".join(available) if available else "(none)"


def _missing_default_model_type_error(models: Dict[str, Any]) -> str:
    return (
        "No model_type was provided and config/llm.yaml does not set "
        "`model.default_model_type`; the model call was not started. "
        "Fix: add `model_type: <type>` to the Agent YAML, or set "
        "`model.default_model_type: <type>` in config/llm.yaml. "
        f"Available model types: {_available_types_text(models)}."
    )


def _unknown_model_type_error(model_type: Any, models: Dict[str, Any]) -> str:
    return (
        f"Model type '{model_type}' is not defined in config/llm.yaml; "
        "the model call was not started. "
        f"Available model types: {_available_types_text(models)}. "
        "Fix: set Agent YAML `model_type` to one of the available types, "
        f"or add `model.{model_type}.model: <provider/model>` in config/llm.yaml."
    )


def _missing_default_target_error(default_type: str, models: Dict[str, Any]) -> str:
    return (
        f"config/llm.yaml sets `model.default_model_type: {default_type}`, "
        "but that model type is not defined; the model call was not started. "
        f"Available model types: {_available_types_text(models)}. "
        f"Fix: change `model.default_model_type` to an available type, "
        f"or add `model.{default_type}.model: <provider/model>`."
    )


class LangfuseSettings(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    enabled: bool = True
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    private_key: str = ""
    secret_key: str = ""

    @model_validator(mode="before")
    @classmethod
    def _resolve_private_key(cls, values: dict) -> dict:
        """Resolve private_key from secret_key if not set."""
        if isinstance(values, dict):
            pk = values.get("private_key") or ""
            sk = values.get("secret_key") or ""
            values["private_key"] = pk or sk
        return values

    def get_actual_private_key(self) -> str:
        return self.private_key or self.secret_key or ""

class LlmModelTypeSettings(BaseModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True, frozen=True)
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = DEFAULT_MODEL_TEMPERATURE
    max_tokens: int | str = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_MODEL_TIMEOUT
    num_retries: int = DEFAULT_MODEL_NUM_RETRIES
    retry_delay: float = DEFAULT_MODEL_RETRY_DELAY
    max_retry_delay: float = DEFAULT_MODEL_MAX_RETRY_DELAY
    extra_headers: Optional[Dict[str, Any]] = None
    context_cache: bool = DEFAULT_MODEL_CONTEXT_CACHE
    system_prompt_boundary: Optional[str] = None
    description: str = ""
    requests_per_minute: int = DEFAULT_MODEL_REQUESTS_PER_MINUTE
    # Extra parameters passed through to litellm.completion() (e.g. reasoning_effort,
    # extra_body). Any YAML key not in the known fields list is collected here.
    extra_completion_params: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_tool_call_detection(cls, values: dict) -> dict:
        if isinstance(values, dict) and "supports_native_tool_calls" in values:
            raise ValueError(
                "supports_native_tool_calls was removed. AgentLoom now sends structured tool schemas "
                "whenever tools are available; remove this field."
            )
        return values

class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    # Internal representation from yaml
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    # the entire "model" block gets split into its pieces
    default_model_type: str = ""
    models: Dict[str, LlmModelTypeSettings] = Field(default_factory=dict)
    
    @classmethod
    def load_from_yaml(cls, path: Path) -> "LLMConfig":
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
            
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LLMConfig":
        langfuse_raw = raw.get("langfuse", {})
        model_raw = raw.get("model", {})
        default_type = model_raw.get("default_model_type", "")
        
        # Build models dict
        models: Dict[str, LlmModelTypeSettings] = {}
        for k, v in model_raw.items():
            if k in _RESERVED_MODEL_KEYS or not isinstance(v, dict):
                continue

            resolved_base_url = v.get("base_url") or ""
            resolved_api_key = v.get("api_key") or ""
            resolved_rpm = v.get("requests_per_minute", DEFAULT_MODEL_REQUESTS_PER_MINUTE)
            resolved_temp = v.get("temperature", DEFAULT_MODEL_TEMPERATURE)
            resolved_max_tokens = v.get("max_tokens", DEFAULT_MAX_TOKENS)
            resolved_timeout = v.get("timeout", DEFAULT_MODEL_TIMEOUT)
            resolved_num_retries = v.get("num_retries", DEFAULT_MODEL_NUM_RETRIES)
            resolved_retry_delay = v.get("retry_delay", DEFAULT_MODEL_RETRY_DELAY)
            resolved_max_retry_delay = v.get("max_retry_delay", DEFAULT_MODEL_MAX_RETRY_DELAY)
            resolved_extra_headers = v.get("extra_headers")

            model_id = v.get("model", "")
            if not model_id:
                raise ValueError(
                    f"Model type '{k}' in llm.yaml is missing required 'model' field. "
                    f"Each model type must specify a LiteLLM model ID "
                    f"(e.g., 'openai/gpt-4o', 'anthropic/claude-3-5-sonnet')."
                )

            if "supports_native_tool_calls" in v:
                raise ValueError(
                    f"Model type '{k}' uses removed field 'supports_native_tool_calls'. "
                    "AgentLoom now always sends structured tool schemas when tools are available; "
                    "remove this field from config/llm.yaml."
                )

            # Collect extra keys not in the known fields list.
            # These are passed through to litellm.completion() as-is.
            _KNOWN_FIELDS = {
                "model", "base_url", "api_key", "temperature", "max_tokens",
                "timeout", "num_retries", "retry_delay", "max_retry_delay",
                "extra_headers", "context_cache", "system_prompt_boundary",
                "description", "requests_per_minute", "supports_structured_output",
            }
            extra_completion_params = {
                ek: ev for ek, ev in v.items() if ek not in _KNOWN_FIELDS
            } or None

            models[k] = LlmModelTypeSettings(
                model=model_id,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                temperature=float(resolved_temp),
                max_tokens=IntParser.parse(resolved_max_tokens, default=DEFAULT_MAX_TOKENS, allow_bypass_strings=("max",)),
                timeout=int(resolved_timeout),
                num_retries=int(resolved_num_retries),
                retry_delay=float(resolved_retry_delay),
                max_retry_delay=float(resolved_max_retry_delay),
                extra_headers=resolved_extra_headers,
                context_cache=BoolParser.parse(v.get("context_cache", DEFAULT_MODEL_CONTEXT_CACHE), default=DEFAULT_MODEL_CONTEXT_CACHE),
                system_prompt_boundary=v.get("system_prompt_boundary", None),
                description=str(v.get("description", f"Model type '{k}' loaded from YAML config")),
                requests_per_minute=int(resolved_rpm),
                extra_completion_params=extra_completion_params,
            )

        # Validate required model types:
        # - 'summary': context compression (smart_summary) depends on it
        if models:
            for required in ("summary",):
                if required not in models:
                    raise ValueError(
                        f"Model type '{required}' is required in llm.yaml but not found. "
                        f"Defined types: {list(models.keys())}"
                    )

        return cls(
            langfuse=LangfuseSettings(**langfuse_raw),
            default_model_type=str(default_type or ""),
            models=models
        )
        
    def to_legacy_dict(self) -> Dict[str, Any]:
        """
        Export back to the nested dict structure expected by the rest of the application
        if arbitrary code tries to read C.raw['model'] or C.raw['langfuse'].
        """
        model_dict = {
            "default_model_type": self.default_model_type
        }
        for k, v in self.models.items():
            model_dict[k] = v.model_dump()
            
        return {
            "langfuse": self.langfuse.model_dump(),
            "model": model_dict
        }

    def for_type(self, model_type: Optional[str]) -> LlmModelTypeSettings:
        desired = (model_type or "").strip().lower()

        # If user explicitly requested a type and it exists, return it.
        if desired and desired in self.models:
            return self.models[desired]

        # If user explicitly requested a type but it DOES NOT exist, raise error immediately.
        if desired:
            raise ValueError(_unknown_model_type_error(model_type, self.models))

        # If user did not request a type (empty/None), use global default_model_type.
        default_type = self.default_model_type.strip().lower()
        if default_type and default_type in self.models:
            return self.models[default_type]

        if not default_type:
            raise ValueError(_missing_default_model_type_error(self.models))

        raise ValueError(_missing_default_target_error(self.default_model_type, self.models))

    @property
    def available_types(self) -> list[str]:
        return list(self.models.keys())
