"""System-level config validation and normalization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.lib.config.model_request_header_profiles import (
    MODEL_REQUEST_HEADER_PROFILE_NAMES,
)


class BoolParser:
    """Utility for tolerant boolean parsing."""

    _TRUTHY_STRINGS = frozenset({"true", "yes", "1", "on", "y"})
    _FALSY_STRINGS = frozenset({"false", "no", "0", "off", "n", ""})

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        default: bool = False,
        field_name: str = "value",
        logger: Any = None,
    ) -> bool:
        """Parse a boolean value with tolerance for common string representations."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            normalised = value.strip().lower()
            if normalised in cls._TRUTHY_STRINGS:
                return True
            if normalised in cls._FALSY_STRINGS:
                return False
        if value is None:
            return default

        if logger is not None:
            from src.lib.logging.logger_manager import get_logger
            log = get_logger(logger, __name__)
            log.warning(
                "Unrecognised boolean for '%s': %r — defaulting to %s",
                field_name,
                value,
                default,
            )
        return default


class IntParser:
    """Utility for tolerant integer parsing."""

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        default: int = 0,
        field_name: str = "value",
        logger: Any = None,
        allow_bypass_strings: tuple[str, ...] = (),
    ) -> int | str:
        """Parse an integer value with tolerance for strings and bypass strings."""
        if isinstance(value, int):
            if isinstance(value, bool):  # bool is subclass of int in Python
                return 1 if value else 0
            return value
        if isinstance(value, str):
            val_str = value.strip()
            if val_str in allow_bypass_strings:
                return val_str
            try:
                return int(val_str)
            except ValueError:
                pass

        if logger is not None:
            from src.lib.logging.logger_manager import get_logger
            log = get_logger(logger, __name__)
            log.warning(
                "Unrecognised integer for '%s': %r — defaulting to %s",
                field_name,
                value,
                default,
            )
        return default


class FloatParser:
    """Utility for tolerant float parsing."""

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        default: float = 0.0,
        field_name: str = "value",
        logger: Any = None,
    ) -> float:
        """Parse a float value with tolerance for integer and string representations."""
        if isinstance(value, float):
            return value
        if isinstance(value, int):
            if isinstance(value, bool):
                return 1.0 if value else 0.0
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass

        if logger is not None:
            from src.lib.logging.logger_manager import get_logger
            log = get_logger(logger, __name__)
            log.warning(
                "Unrecognised float for '%s': %r — defaulting to %s",
                field_name,
                value,
                default,
            )
        return default


class EnumParser:
    """Utility for tolerant choice parsing."""

    @classmethod
    def parse(
        cls,
        value: Any,
        choices_map: dict[str, Any],
        *,
        default: Any = None,
        ignore_case: bool = True,
        field_name: str = "value",
        logger: Any = None,
    ) -> Any:
        """Parse a choice based on a mapping, returning default if unrecognized."""
        if value is None:
            return default

        if isinstance(value, str):
            normalized = value.strip()
            if ignore_case:
                normalized = normalized.upper()

            if normalized in choices_map:
                return choices_map[normalized]
        else:
            # Also allow direct integer or object values if they exactly match values in choices_map
            # But normally choices_map keys are strings.
            pass

        if logger is not None:
            from src.lib.logging.logger_manager import get_logger
            log = get_logger(logger, __name__)
            log.warning(
                "Unrecognised choice for '%s': %r — defaulting to %r",
                field_name,
                value,
                default,
            )
        return default


class LogLevelParser:
    """Utility for parsing log levels."""

    OFF_LEVEL = logging.CRITICAL + 10

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        default: int = logging.INFO,
        field_name: str = "level",
        logger: Any = None,
    ) -> int:
        """Parse log level strings or integers into integer logging constants."""
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            normalized = value.strip().upper()
            if not normalized:
                return default
            if normalized == "OFF":
                return cls.OFF_LEVEL
            if normalized in logging._nameToLevel:
                return int(logging._nameToLevel[normalized])

        if logger is not None:
            from src.lib.logging.logger_manager import get_logger
            log = get_logger(logger, __name__)
            log.warning(
                "Unrecognised log level for '%s': %r — defaulting to %r",
                field_name,
                value,
                default,
            )
        return default


class SystemSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = "AgentLoom"
    version: str = "1.0.1"
    user_agent: str = "AgentLoom/1.0.1"


class ModelRequestHeadersSettings(BaseModel):
    """System-level default headers for outbound model API requests."""

    model_config = ConfigDict(extra="allow")
    profile: str = "agentloom"
    profiles: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, value: str) -> str:
        return str(value or "agentloom").strip().lower()

    @field_validator("headers", "profiles", mode="before")
    @classmethod
    def _validate_mapping(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("model_request_headers headers/profiles must be mappings")
        return value

    @model_validator(mode="after")
    def _validate_selected_profile(self) -> "ModelRequestHeadersSettings":
        builtin_profiles = {
            "agentloom",
            "generic",
            "none",
        } | MODEL_REQUEST_HEADER_PROFILE_NAMES
        custom_profiles = {str(name).strip().lower() for name in self.profiles}
        if self.profile not in builtin_profiles and self.profile not in custom_profiles:
            allowed_text = ", ".join(sorted(builtin_profiles | custom_profiles))
            raise ValueError(
                f"model_request_headers.profile must be built-in or configured under "
                f"model_request_headers.profiles: {allowed_text}"
            )
        return self


class PathValidationRule(BaseModel):
    """A single path-validation rule targeting a group of tools."""
    model_config = ConfigDict(extra="allow")
    tools: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    include_paths: list[str] = Field(default_factory=list)
    path_param_patterns: list[str] = Field(default_factory=list)


class ToolAccessControlSettings(BaseModel):
    """Tool access control settings.

    All path access rules are defined via ``path_validation`` entries.
    There are no global include/exclude fields — each rule specifies
    its own ``include_paths`` and ``exclude_paths`` for the tools it
    covers.  ``include_paths`` / ``exclude_paths`` support tilde (``~``)
    expansion, glob patterns (``fnmatch``), and the wildcard ``"*"``
    (match everything).  When a path matches both include and exclude,
    **exclude takes priority** (security-first).
    """
    model_config = ConfigDict(extra="allow")
    path_validation: list[PathValidationRule] = Field(default_factory=list)


class RuntimeSettings(BaseModel):
    """Canonical runtime-home and retention settings."""

    model_config = ConfigDict(extra="forbid")
    root_dir: str = ".agentloom"
    successful_run_retention_days: int = Field(default=7, ge=0)
    failed_run_retention_days: int = Field(default=30, ge=0)
    artifact_retention_days: int = Field(default=3, ge=0)
    cleanup_interval_hours: int = Field(default=24, ge=24)

    @field_validator("root_dir")
    @classmethod
    def _validate_root_dir(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("runtime.root_dir must not be empty")
        return cleaned


class LoggingSettings(BaseModel):
    """Run-scoped logging settings with bounded file retention."""

    model_config = ConfigDict(extra="forbid")
    level: str | int = "INFO"
    console_enabled: bool = True
    file_enabled: bool = True
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    backup_count: int = Field(default=3, ge=0)


class RootSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    system: SystemSettings = Field(default_factory=SystemSettings)
    model_request_headers: ModelRequestHeadersSettings = Field(default_factory=ModelRequestHeadersSettings)
    tool_access_control: ToolAccessControlSettings = Field(default_factory=ToolAccessControlSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    smart_summary: bool = True
    context_engine: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] = Field(default_factory=dict)
    execution_env: dict[str, Any] = Field(default_factory=dict)
    code_agent: dict[str, Any] = Field(default_factory=dict)
    tools: list[Any] = Field(default_factory=list)
    default_toolsets: list[str] = Field(default_factory=list)
    toolsets: list[str] = Field(default_factory=list)
    shell_settings: dict[str, Any] = Field(default_factory=dict)
    tools_mapping: dict[str, Any] = Field(default_factory=dict)
    tool_metadata: dict[str, Any] = Field(default_factory=dict)
    tool_output_limits: dict[str, Any] = Field(default_factory=dict)
    self_learning: dict[str, Any] = Field(default_factory=dict)


def raise_project_key_error(source: str) -> ValueError:
    return ValueError(
        f"Unsupported top-level key 'project' in {source}. "
        "Use 'tool_access_control' instead."
    )


def validate_system_snapshot(snapshot: dict[str, Any], source: str) -> None:
    if "project" in snapshot:
        raise raise_project_key_error(source)
    if "default_loaded_tools" in snapshot:
        raise ValueError(
            f"Unsupported top-level key 'default_loaded_tools' in {source}. "
            "Use 'default_toolsets' in system config or 'toolsets' in Agent YAML."
        )
    self_learning = snapshot.get("self_learning")
    if isinstance(self_learning, dict) and "root_dir" in self_learning:
        raise ValueError(
            f"Unsupported key 'self_learning.root_dir' in {source}. "
            "Use the single canonical 'runtime.root_dir'."
        )
    tools_mapping = snapshot.get("tools_mapping")
    if isinstance(tools_mapping, dict) and "mapping" in tools_mapping:
        raise ValueError(
            f"Unsupported key 'tools_mapping.mapping' in {source}. "
            "Use platform keys such as 'tools_mapping.Claude'."
        )
    RootSettings.model_validate(snapshot)


def normalize_tool_access_control_section(raw: dict[str, Any], agent_root: Path) -> None:
    tac_cfg = raw.setdefault("tool_access_control", {})
    if not isinstance(tac_cfg, dict):
        raise ValueError("tool_access_control must be a mapping")
