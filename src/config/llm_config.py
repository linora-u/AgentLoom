"""
Independent Parsing for LLM Configuration (model.yaml)
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from agentloom.config.config_validation import BoolParser, IntParser
from agentloom.config.defaults import (
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
from agentloom.config.model_adapters import MODEL_ADAPTERS, AdapterKind
from agentloom.config.output_budget import UNSET_BUDGET, input_plan, positive_budget, resolve_cap
from agentloom.config.yaml_loader import load_unique_yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_RESERVED_MODEL_KEYS = {"default_model_type", "common"}


def _available_types_text(models: dict[str, Any]) -> str:
    available = list(models.keys())
    return ", ".join(available) if available else "(none)"


def _missing_default_model_type_error(models: dict[str, Any]) -> str:
    return (
        "No model_type was provided and config/model.yaml does not set "
        "`model.default_model_type`; the model call was not started. "
        "Fix: add `model_type: <type>` to the Agent YAML, or set "
        "`model.default_model_type: <type>` in config/model.yaml. "
        f"Available model types: {_available_types_text(models)}."
    )


def _unknown_model_type_error(model_type: Any, models: dict[str, Any]) -> str:
    return (
        f"Model type '{model_type}' is not defined in config/model.yaml; "
        "the model call was not started. "
        f"Available model types: {_available_types_text(models)}. "
        "Fix: set Agent YAML `model_type` to one of the available types, "
        f"or add `model.{model_type}.model: <provider/model>` in config/model.yaml."
    )


def _missing_default_target_error(default_type: str, models: dict[str, Any]) -> str:
    return (
        f"config/model.yaml sets `model.default_model_type: {default_type}`, "
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
    adapter: AdapterKind
    base_url: str = ""
    api_key: str = ""
    temperature: float = DEFAULT_MODEL_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    context_window: int = DEFAULT_MAX_TOKENS
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    input_token_limit: int = DEFAULT_MAX_TOKENS - DEFAULT_MAX_OUTPUT_TOKENS
    input_token_reserve: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1, strict=True)
    input_reserve_source: str = "local_input_reserve_policy"
    output_cap_source: str = "omitted"
    max_model_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    model_output_capability_source: str | None = None
    timeout: int = DEFAULT_MODEL_TIMEOUT
    num_retries: int = DEFAULT_MODEL_NUM_RETRIES
    retry_delay: float = DEFAULT_MODEL_RETRY_DELAY
    max_retry_delay: float = DEFAULT_MODEL_MAX_RETRY_DELAY
    extra_headers: dict[str, Any] | None = None
    context_cache: bool = DEFAULT_MODEL_CONTEXT_CACHE
    system_prompt_boundary: str | None = None
    description: str = ""
    requests_per_minute: int = DEFAULT_MODEL_REQUESTS_PER_MINUTE
    supports_structured_output: bool = False
    # Extra parameters passed through to litellm.completion() (e.g. reasoning_effort,
    # extra_body). Any YAML key not in the known fields list is collected here.
    extra_completion_params: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _budget_contract(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        extra = dict(values.get("extra_completion_params") or {})
        if "max_completion_tokens" in values:
            extra["max_completion_tokens"] = values.pop("max_completion_tokens")
        cap, extra, source = resolve_cap(values.get("max_output_tokens", UNSET_BUDGET), extra,
                                        source=values.get("output_cap_source"))
        capability = positive_budget(values.get("max_model_output_tokens"), "max_model_output_tokens")
        capability_source = values.get("model_output_capability_source")
        if capability is not None and not (isinstance(capability_source, str) and capability_source.strip()):
            raise ValueError("max_model_output_tokens requires model_output_capability_source from verified Provider documentation")
        if cap is not None and capability is not None and cap > capability:
            raise ValueError("max_output_tokens exceeds verified max_model_output_tokens")
        if values.get("adapter") == "openai_chatgpt_responses" and cap is not None:
            raise ValueError("openai_chatgpt_responses does not support an explicit output cap")
        if values.get("adapter") == "anthropic_messages" and cap is None:
            if capability is None:
                raise ValueError("anthropic_messages requires max_output_tokens or verified max_model_output_tokens")
            cap, source = capability, "verified_model_capability"
        reserve, limit, reserve_source = input_plan(
            values.get("context_window", DEFAULT_MAX_TOKENS), cap,
            values.get("input_token_reserve", UNSET_BUDGET), legacy=source == "legacy_max_tokens")
        values.update(max_output_tokens=cap, extra_completion_params=extra, output_cap_source=source,
                      input_token_reserve=reserve,
                      input_reserve_source=values.get("input_reserve_source", reserve_source))
        if "input_token_limit" not in values:
            values["input_token_limit"] = limit
        elif type(values["input_token_limit"]) is not int or values["input_token_limit"] <= 0 or values["input_token_limit"] > limit:
            raise ValueError("input_token_limit exceeds context_window minus required input reserve")
        return values

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
    models: dict[str, LlmModelTypeSettings] = Field(default_factory=dict)

    @classmethod
    def load_from_yaml(cls, path: Path) -> LLMConfig:
        cls._check_filename_migration(path)
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as f:
            raw = load_unique_yaml(f) or {}

        return cls.from_dict(raw)

    @staticmethod
    def _check_filename_migration(path: Path) -> None:
        if path.name == "model.yaml":
            for old_name in ("llm.yaml", "llm.yml"):
                old_path = path.with_name(old_name)
                if old_path.exists():
                    raise ValueError(f"{old_path} is no longer supported; migrate it to {path} and remove the old file")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LLMConfig:
        if not isinstance(raw, dict):
            raise ValueError("model.yaml must be a mapping")
        langfuse_raw = raw.get("langfuse", {})
        model_raw = raw.get("model", {})
        if not isinstance(model_raw, dict) or not isinstance(langfuse_raw, dict):
            raise ValueError("model.yaml model and langfuse sections must be mappings")
        default_type = model_raw.get("default_model_type", "")

        # Build models dict
        models: dict[str, LlmModelTypeSettings] = {}
        for k, v in model_raw.items():
            if k in _RESERVED_MODEL_KEYS or not isinstance(v, dict):
                continue

            resolved_base_url = v.get("base_url") or ""
            resolved_api_key = v.get("api_key") or ""
            resolved_rpm = v.get("requests_per_minute", DEFAULT_MODEL_REQUESTS_PER_MINUTE)
            resolved_temp = v.get("temperature", DEFAULT_MODEL_TEMPERATURE)
            resolved_max_tokens = v.get("max_tokens", DEFAULT_MAX_TOKENS)
            parsed_max_tokens = (
                DEFAULT_MAX_TOKENS
                if isinstance(resolved_max_tokens, str) and resolved_max_tokens.strip() == "max"
                else int(
                    IntParser.parse(
                        resolved_max_tokens,
                        default=DEFAULT_MAX_TOKENS,
                    )
                )
            )
            has_legacy_budget = "max_tokens" in v and "context_window" not in v and "max_output_tokens" not in v
            resolved_context_window = parsed_max_tokens if has_legacy_budget else int(IntParser.parse(
                v.get("context_window", DEFAULT_MAX_TOKENS), default=DEFAULT_MAX_TOKENS))
            resolved_max_output_tokens = parsed_max_tokens if has_legacy_budget else v.get("max_output_tokens", UNSET_BUDGET)
            resolved_timeout = v.get("timeout", DEFAULT_MODEL_TIMEOUT)
            resolved_num_retries = v.get("num_retries", DEFAULT_MODEL_NUM_RETRIES)
            resolved_retry_delay = v.get("retry_delay", DEFAULT_MODEL_RETRY_DELAY)
            resolved_max_retry_delay = v.get("max_retry_delay", DEFAULT_MODEL_MAX_RETRY_DELAY)
            resolved_extra_headers = v.get("extra_headers")

            model_id = v.get("model", "")
            if not model_id:
                raise ValueError(
                    f"Model type '{k}' in model.yaml is missing required 'model' field. "
                    f"Each model type must specify a LiteLLM model ID "
                    f"(e.g., 'openai/gpt-4o', 'anthropic/claude-3-5-sonnet')."
                )

            adapter = v.get("adapter")
            if adapter is None:
                raise ValueError(
                    f"Model type '{k}' in model.yaml is missing required 'adapter' field. "
                    f"Choose one of: {', '.join(MODEL_ADAPTERS)}."
                )
            if adapter not in MODEL_ADAPTERS:
                raise ValueError(
                    f"Model type '{k}' in model.yaml has unsupported adapter {adapter!r}. "
                    f"Choose one of: {', '.join(MODEL_ADAPTERS)}."
                )

            if "supports_native_tool_calls" in v:
                raise ValueError(
                    f"Model type '{k}' uses removed field 'supports_native_tool_calls'. "
                    "AgentLoom now always sends structured tool schemas when tools are available; "
                    "remove this field from config/model.yaml."
                )

            # Collect extra keys not in the known fields list.
            # These are passed through to litellm.completion() as-is.
            _KNOWN_FIELDS = {
                "model", "adapter", "base_url", "api_key", "temperature", "max_tokens",
                "context_window", "max_output_tokens", "input_token_limit", "input_token_reserve",
                "input_reserve_source", "output_cap_source", "max_model_output_tokens", "model_output_capability_source", "extra_completion_params",
                "timeout", "num_retries", "retry_delay", "max_retry_delay",
                "extra_headers", "context_cache", "system_prompt_boundary",
                "description", "requests_per_minute", "supports_structured_output",
            }
            extra_completion_params = dict(v.get("extra_completion_params") or {})
            extra_completion_params.update({ek: ev for ek, ev in v.items() if ek not in _KNOWN_FIELDS})
            resolved_max_output_tokens, extra_completion_params, cap_source = resolve_cap(
                resolved_max_output_tokens, extra_completion_params,
                source="legacy_max_tokens" if has_legacy_budget else v.get("output_cap_source"))
            if adapter == "anthropic_messages" and resolved_max_output_tokens is None and v.get("max_model_output_tokens") is not None:
                resolved_max_output_tokens = positive_budget(v["max_model_output_tokens"], "max_model_output_tokens")
                cap_source = "verified_model_capability"
            reserve, resolved_input_token_limit, reserve_source = input_plan(
                resolved_context_window, resolved_max_output_tokens,
                v.get("input_token_reserve", UNSET_BUDGET), legacy=cap_source == "legacy_max_tokens")
            if "input_token_limit" in v:
                resolved_input_token_limit = v["input_token_limit"]

            models[k] = LlmModelTypeSettings(
                model=model_id,
                adapter=adapter,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                temperature=float(resolved_temp),
                max_tokens=parsed_max_tokens,
                context_window=resolved_context_window,
                max_output_tokens=resolved_max_output_tokens,
                input_token_limit=resolved_input_token_limit,
                input_token_reserve=reserve,
                input_reserve_source=v.get("input_reserve_source", reserve_source),
                output_cap_source=cap_source,
                max_model_output_tokens=v.get("max_model_output_tokens"),
                model_output_capability_source=v.get("model_output_capability_source"),
                timeout=int(resolved_timeout),
                num_retries=int(resolved_num_retries),
                retry_delay=float(resolved_retry_delay),
                max_retry_delay=float(resolved_max_retry_delay),
                extra_headers=resolved_extra_headers,
                context_cache=BoolParser.parse(v.get("context_cache", DEFAULT_MODEL_CONTEXT_CACHE), default=DEFAULT_MODEL_CONTEXT_CACHE),
                system_prompt_boundary=v.get("system_prompt_boundary", None),
                description=str(v.get("description", f"Model type '{k}' loaded from YAML config")),
                requests_per_minute=int(resolved_rpm),
                supports_structured_output=BoolParser.parse(
                    v.get("supports_structured_output", False), default=False,
                ),
                extra_completion_params=extra_completion_params,
            )

        return cls(
            langfuse=LangfuseSettings(**langfuse_raw),
            default_model_type=str(default_type or ""),
            models=models
        )

    def overlay_from_yaml(self, path: Path) -> LLMConfig:
        """Replace complete named profiles; retain unspecified project models."""
        self._check_filename_migration(path)
        if not path.exists():
            return self.model_copy(deep=True)
        raw = load_unique_yaml(path.read_text(encoding="utf-8")) or {}
        overlay = self.from_dict(raw)
        changes: dict[str, Any] = {"models": {**self.models, **overlay.models}}
        if "default_model_type" in raw.get("model", {}):
            changes["default_model_type"] = overlay.default_model_type
        return self.model_copy(deep=True, update=deepcopy(changes))

    def patch_from_yaml(self, path: Path) -> LLMConfig:
        """Patch fields of named profiles without copying public model settings."""
        if not path.exists():
            return self.model_copy(deep=True)
        raw = load_unique_yaml(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict) or set(raw) != {"model"}:
            raise ValueError(f"{path.name} must contain only a model mapping")
        patches = raw["model"]
        if not isinstance(patches, dict):
            raise ValueError(f"{path.name} model must be a mapping")
        models = dict(self.models)
        for name, patch in patches.items():
            if name in _RESERVED_MODEL_KEYS:
                raise ValueError(f"{path.name} cannot patch reserved model key {name!r}")
            if not isinstance(patch, dict):
                raise ValueError(f"{path.name} model profile {name!r} must be a mapping")
            current = models.get(name)
            if current is None:
                merged = dict(patch)
            else:
                merged = current.model_dump(
                    exclude={"extra_completion_params", "input_token_limit"}
                )
                merged.update(current.extra_completion_params or {})
                merged.update(patch)
                if "input_token_reserve" in patch:
                    merged.pop("input_reserve_source", None)
                if "max_output_tokens" in patch or "max_completion_tokens" in patch:
                    merged.pop("output_cap_source", None)
                    if current.output_cap_source == "legacy_max_tokens" and "input_token_reserve" not in patch:
                        merged.pop("input_token_reserve", None)
                        merged.pop("input_reserve_source", None)
            models[name] = self.from_dict({"model": {name: merged}}).models[name]
        return self.model_copy(deep=True, update={"models": deepcopy(models)})

    def validate_runtime(self, runtime_id: str, options: dict[str, Any]) -> None:
        """Only smol smart compression requires a separate summary profile."""
        if runtime_id != "smolagents" or not options.get("smart_summary", True):
            return
        if "summary" not in self.models:
            raise ValueError("Model type 'summary' is required in model.yaml when smolagents smart_summary is enabled")
        if self.models["summary"].adapter in {"openai_codex_responses", "openai_chatgpt_responses"}:
            raise ValueError("smolagents summary model requires a Python model adapter; Codex and ChatGPT adapters require Pi")

    def to_legacy_dict(self) -> dict[str, Any]:
        """
        Export back to the nested dict structure expected by the rest of the application
        if arbitrary code tries to read C.raw['model'] or C.raw['langfuse'].
        """
        model_dict: dict[str, Any] = {
            "default_model_type": self.default_model_type
        }
        for k, v in self.models.items():
            model_dict[k] = v.model_dump()

        return {
            "langfuse": self.langfuse.model_dump(),
            "model": model_dict
        }

    def for_type(self, model_type: str | None) -> LlmModelTypeSettings:
        desired = (model_type or "").strip().lower()

        # If user explicitly requested a type and it exists, return it.
        if desired and desired in self.models:
            return self.models[desired]

        # If user explicitly requested a type but it DOES NOT exist, raise error immediately.
        if desired:
            raise ValueError(_unknown_model_type_error(model_type, self.models))

        # If user did not request a type (empty/None), use the effective default_model_type.
        default_type = self.default_model_type.strip().lower()
        if default_type and default_type in self.models:
            return self.models[default_type]

        if not default_type:
            raise ValueError(_missing_default_model_type_error(self.models))

        raise ValueError(_missing_default_target_error(self.default_model_type, self.models))

    @property
    def available_types(self) -> list[str]:
        return list(self.models.keys())
