"""
Unified configuration management.

Configuration precedence (low -> high):
1. config/system.yaml
2. config/llm.yaml
3. applications/<app>/config/system.yaml  (optional, resolved via Agent YAML path)
"""

from __future__ import annotations

import builtins
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from src.lib.logging import get_logger

from .config_validation import (
    RootSettings,
    normalize_tool_access_control_section,
    raise_project_key_error,
    validate_system_snapshot,
)
from .defaults import DEFAULT_MODEL_REQUESTS_PER_MINUTE
from .layered_builder import LayeredConfigBuilder
from .llm_config import LLMConfig

SYSTEM_CONFIG_NAME = "system.yaml"
LLM_CONFIG_NAME = "llm.yaml"
_CAMPAIGN_LLM_CONFIG_FD_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_FD"
_MAX_CAMPAIGN_LLM_CONFIG_BYTES = 1024 * 1024
APP_CONFIG_RELATIVE_PATH = Path("config") / SYSTEM_CONFIG_NAME
_PROJECT_NAME = "AgentLoom"
_WORKFLOW_OVERLAY_KEYS = {
    "system",
    "model_request_headers",
    "smart_summary",
    "context_engine",
    "tool_access_control",
    "execution_env",
    "code_agent",
    "tools",
    "shell_settings",
    "tools_mapping",
    "default_toolsets",
    "toolsets",
    "prompt",
    "mcp_servers",
    "self_learning",
}
_LLM_ONLY_TOP_LEVEL_KEYS = {"model", "llm", "langfuse"}

logger = get_logger(__name__)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration file must contain a mapping: {path}")
    return loaded


def _load_llm_config(path: Path) -> LLMConfig:
    """Load one capsule-only config pipe, otherwise use the normal disk file.

    Only the numeric descriptor is transported in the environment.  The
    credential-bearing payload is consumed exactly once and the descriptor is
    closed before any tool or provider subprocess can inherit it.
    """
    fd_value = ""
    if os.environ.get("AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_ACTIVE") == "1":
        fd_value = str(os.environ.pop(_CAMPAIGN_LLM_CONFIG_FD_ENV, "") or "")
    if not fd_value:
        return LLMConfig.load_from_yaml(path)
    try:
        fd = int(fd_value)
        if fd < 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError("invalid in-memory campaign LLM configuration") from exc
    try:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            raw_bytes = stream.read(_MAX_CAMPAIGN_LLM_CONFIG_BYTES + 1)
        if len(raw_bytes) > _MAX_CAMPAIGN_LLM_CONFIG_BYTES:
            raise ValueError("in-memory campaign LLM configuration is too large")
        raw = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("invalid in-memory campaign LLM configuration") from exc
    if not isinstance(raw, dict):
        raise ValueError("in-memory campaign LLM configuration must be a mapping")
    return LLMConfig.from_dict(raw)


def _filter_llm_only_top_level_keys(
    config_map: dict[str, Any] | None,
    *,
    source_name: str,
) -> dict[str, Any]:
    if not config_map:
        return {}

    filtered: dict[str, Any] = {}
    for key, value in config_map.items():
        if key in _LLM_ONLY_TOP_LEVEL_KEYS:
            logger.warning(
                "Ignoring top-level key '%s' in %s; LLM settings must come from config/%s only.",
                key,
                source_name,
                LLM_CONFIG_NAME,
            )
            continue
        filtered[key] = value
    return filtered


def _is_agentloom_project(pyproject_path: Path) -> bool:
    """Return *True* if *pyproject_path* declares ``project.name == _PROJECT_NAME``."""
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("name") == _PROJECT_NAME
    except Exception:
        return False


def _discover_agent_root(config_dir: Path | str | None = None) -> Path:
    """Locate the project root directory.

    Detection strategy:

    * When *config_dir* is given (e.g. from tests), it is used directly –
      expected to point at the ``config/`` directory that contains
      ``system.yaml``.
    * Otherwise the function walks **upward** from the current working
      directory and from the package source tree (``__file__``) looking for
      a ``pyproject.toml`` whose ``[project] name`` equals
      :data:`_PROJECT_NAME` (``AgentLoom``).  This avoids false matches
      with sub-projects that also ship a ``config/system.yaml``.
    """
    if config_dir is not None:
        configured_dir = Path(config_dir).expanduser().resolve()
        system_file = configured_dir / SYSTEM_CONFIG_NAME
        if not system_file.exists():
            raise FileNotFoundError(
                f"Configured config directory '{configured_dir}' does not "
                f"contain '{SYSTEM_CONFIG_NAME}'. Please verify the path."
            )
        return configured_dir.parent

    search_origins = [
        ("current working directory", Path.cwd().resolve()),
        ("source tree", Path(__file__).resolve().parent),
    ]
    for _label, start in search_origins:
        current = start
        while current != current.parent:
            candidate = current / "pyproject.toml"
            if candidate.exists() and _is_agentloom_project(candidate):
                return current
            current = current.parent

    searched = ", ".join(f"{label} ({path})" for label, path in search_origins)
    raise FileNotFoundError(
        f"Cannot locate the {_PROJECT_NAME} project root. "
        f"Searched upward from: {searched}. "
        f"Expected to find a pyproject.toml with [project] name = '{_PROJECT_NAME}'. "
        f"Hint: either run from within the project tree or install the package "
        f"in editable mode ('pip install -e .')."
    )


def _resolve_app_root_from_yaml(agent_root: Path, yaml_config_path: Path) -> Path:
    """Determine the application root from an Agent YAML file path.

    Walks **upward** from *yaml_config_path* looking for a directory whose
    name is ``workflows``.  The parent of that directory is the application
    root.  If the resolved app root lives under ``<agent_root>/applications/``
    and contains an optional ``config/system.yaml``, that file will be used
    as an application-level overlay by :func:`build_effective_agent_config`.

    Raises:
        ValueError: If no ``workflows/`` directory can be found in the
            ancestor chain — every application **must** have one.
    """
    current = yaml_config_path.resolve().parent
    while current != current.parent:
        if current.name == "workflows":
            app_root = current.parent
            # Verify app_root is under agent_root/applications/
            applications_root = agent_root / "applications"
            try:
                app_root.relative_to(applications_root)
                return app_root
            except ValueError:
                return agent_root
        if current == agent_root:
            break
        current = current.parent

    raise ValueError(
        f"Cannot locate 'workflows/' directory from {yaml_config_path}. "
        "Every application must contain a workflows/ directory."
    )


class UnifiedConfig:
    def __init__(self, raw: dict[str, Any], agent_root: Path, llm_config: LLMConfig):
        normalized = dict(raw)
        if "project" in normalized:
            raise raise_project_key_error("merged config")

        normalize_tool_access_control_section(normalized, agent_root)
        validate_system_snapshot(normalized, "merged config")

        self._raw = normalized
        self._settings = RootSettings.model_validate(self._raw)
        self._agent_root = agent_root
        self._llm_config = llm_config

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    @property
    def agent_root(self) -> Path:
        return self._agent_root

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    def get_nested(self, *keys: str, default: Any = None) -> Any:
        value: Any = self._raw
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def get_model_config(self, model_type: str, key: str, default: Any = None) -> Any:
        specific = self._llm_config.models.get(model_type)
        if specific is not None and hasattr(specific, key):
            val = getattr(specific, key)
            if val is not None and val != "":
                return val
        return default

    @property
    def system_name(self) -> str:
        return self._settings.system.name

    @property
    def system_version(self) -> str:
        return self._settings.system.version

    @property
    def user_agent(self) -> str:
        return self._settings.system.user_agent

    @property
    def default_model_type(self) -> str:
        return self._llm_config.default_model_type

    @property
    def requests_per_minute(self) -> int:
        try:
            return self._llm_config.for_type(None).requests_per_minute
        except ValueError:
            return DEFAULT_MODEL_REQUESTS_PER_MINUTE

    @property
    def llm_base_url(self) -> str:
        try:
            return self._llm_config.for_type(None).base_url
        except ValueError:
            return ""

    @property
    def llm_api_key(self) -> str:
        try:
            api_key = self._llm_config.for_type(None).api_key
        except ValueError:
            api_key = ""
        return api_key if api_key else "not_provided"

    @property
    def llm(self) -> LLMConfig:
        return self._llm_config

    @property
    def langfuse_host(self) -> str:
        return self._llm_config.langfuse.host

    @property
    def langfuse_public_key(self) -> str:
        return self._llm_config.langfuse.public_key

    @property
    def langfuse_private_key(self) -> str:
        return self._llm_config.langfuse.private_key


_ACTIVE_CONFIG: UnifiedConfig | None = None


def _load_merged_config(config_dir: Path | str | None = None) -> UnifiedConfig:
    """Load global base configuration: ``config/system.yaml`` + ``config/llm.yaml``.

    Application-level overlays (``applications/<app>/config/system.yaml``) are
    **not** handled here — they are resolved later in
    :func:`build_effective_agent_config` using the Agent YAML file path.
    """
    agent_root = _discover_agent_root(config_dir=config_dir)

    config_root = agent_root / "config"
    layered_builder = LayeredConfigBuilder(
        validate_hook=lambda snapshot, overlay: validate_system_snapshot(snapshot, overlay.name)
    )
    system_yaml = _filter_llm_only_top_level_keys(
        _load_yaml(config_root / SYSTEM_CONFIG_NAME),
        source_name="config/system.yaml",
    )
    llm_config = _load_llm_config(config_root / LLM_CONFIG_NAME)

    layered_builder.apply_mapping("config/system.yaml", system_yaml)

    merged = layered_builder.build()
    return UnifiedConfig(merged, agent_root=agent_root, llm_config=llm_config)


def get_config() -> UnifiedConfig:
    global _ACTIVE_CONFIG
    if _ACTIVE_CONFIG is None:
        _ACTIVE_CONFIG = _load_merged_config()
    return _ACTIVE_CONFIG


class ConfigProxy:
    def __getitem__(self, key: str) -> Any:
        return get_config().raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return get_config().get(key, default=default)

    def get_nested(self, *keys: str, default: Any = None) -> Any:
        return get_config().get_nested(*keys, default=default)

    def get_model_config(self, model_type: str, key: str, default: Any = None) -> Any:
        return get_config().get_model_config(model_type, key, default=default)

    @property
    def llm(self) -> LLMConfig:
        return get_config().llm

    @property
    def raw(self) -> dict[str, Any]:
        return get_config().raw

    @property
    def agent_root(self) -> Path:
        return get_config().agent_root

    @property
    def system_name(self) -> str:
        return get_config().system_name

    @property
    def system_version(self) -> str:
        return get_config().system_version

    @property
    def user_agent(self) -> str:
        return get_config().user_agent

    @property
    def default_model_type(self) -> str:
        return get_config().default_model_type

    @property
    def requests_per_minute(self) -> int:
        return get_config().requests_per_minute

    @property
    def llm_base_url(self) -> str:
        return get_config().llm_base_url

    @property
    def llm_api_key(self) -> str:
        return get_config().llm_api_key

    @property
    def langfuse_host(self) -> str:
        return get_config().langfuse_host

    @property
    def langfuse_public_key(self) -> str:
        return get_config().langfuse_public_key

    @property
    def langfuse_private_key(self) -> str:
        return get_config().langfuse_private_key


C = ConfigProxy()


def extract_workflow_overlay(
    config_map: dict[str, Any],
    *,
    source_name: str = "workflow config",
) -> dict[str, Any]:
    if "project" in config_map:
        source = str(config_map.get("_yaml_file_path") or config_map.get("name") or "workflow config")
        raise raise_project_key_error(source)

    filtered_map = _filter_llm_only_top_level_keys(config_map, source_name=source_name)
    overlay: dict[str, Any] = {}
    for key in _WORKFLOW_OVERLAY_KEYS:
        if key not in filtered_map:
            continue

        value = filtered_map[key]
        if key == "prompt":
            if isinstance(value, (str, dict)):
                overlay[key] = value
            continue

        if key == "tools":
            if isinstance(value, list):
                overlay[key] = value
            continue

        if key in ("shell_settings", "tools_mapping", "default_toolsets", "toolsets"):
            overlay[key] = value
            continue

        if key == "smart_summary":
            overlay[key] = value
            continue

        # mcp_servers: pass through as-is (string, list, or dict all valid)
        if key == "mcp_servers":
            overlay[key] = value
            continue

        if isinstance(value, dict):
            overlay[key] = value
    return overlay


def build_effective_agent_config(
    agent_config: dict[str, Any] | None,
    *,
    source_name: str = "agent",
) -> dict[str, Any]:
    """Build the final merged config for a single Agent.

    Merge order (low → high):
    1. Global base (``config/system.yaml`` + ``config/llm.yaml``)
    2. Application overlay (``<app_root>/config/system.yaml``, optional)
    3. Agent YAML overlay

    The application root is determined by walking **upward** from the Agent
    YAML file path (stored in ``agent_config["_yaml_file_path"]``) until a
    ``workflows/`` directory is found.
    """
    base = get_config()
    layered_builder = LayeredConfigBuilder(
        validate_hook=lambda snapshot, overlay: validate_system_snapshot(snapshot, overlay.name)
    )
    layered_builder.apply_mapping("base_config", deepcopy(base.raw))

    # --- Application-level overlay (resolved from _yaml_file_path) ---
    if isinstance(agent_config, dict):
        yaml_file_path = agent_config.get("_yaml_file_path")
        if yaml_file_path:
            try:
                app_root = _resolve_app_root_from_yaml(
                    base.agent_root, Path(yaml_file_path)
                )
                app_config_path = app_root / APP_CONFIG_RELATIVE_PATH
                if app_root != base.agent_root and app_config_path.exists():
                    app_system_yaml = _filter_llm_only_top_level_keys(
                        _load_yaml(app_config_path),
                        source_name=str(app_config_path),
                    )
                    layered_builder.apply_mapping(
                        str(app_config_path), app_system_yaml
                    )
            except ValueError:
                logger.warning(
                    "Skipped application config discovery for '%s': "
                    "no workflows/ directory found.",
                    yaml_file_path,
                )

    # --- Agent YAML overlay ---
    if isinstance(agent_config, dict):
        layered_builder.apply_mapping(
            source_name,
            extract_workflow_overlay(deepcopy(agent_config), source_name=source_name),
        )
    merged = layered_builder.build()
    normalize_tool_access_control_section(merged, base.agent_root)
    validate_system_snapshot(merged, source_name)
    if isinstance(agent_config, dict) and agent_config.get("_yaml_file_path"):
        # Identity metadata, not configuration: application-scope resolution
        # (self-learning memory layering, learning artifacts) reads the workflow
        # path from the effective config at hook time.
        merged["_yaml_file_path"] = str(agent_config["_yaml_file_path"])
    return merged


def get_model_config(model_type: str, key: str, default: Any = None) -> Any:
    return get_config().get_model_config(model_type, key, default=default)


def get_default_toolsets(config_map: dict[str, Any] | None = None) -> list[str]:
    if config_map is not None:
        toolsets = config_map.get("default_toolsets", [])
    else:
        toolsets = C.get("default_toolsets", [])

    if not isinstance(toolsets, list):
        return []
    return [toolset for toolset in toolsets if isinstance(toolset, str) and toolset.strip()]


def get_code_agent_config(config_map: dict[str, Any] | None = None) -> dict[str, Any]:
    if config_map is not None:
        code_cfg = config_map.get("code_agent", {})
    else:
        code_cfg = C.get("code_agent", {})

    if not isinstance(code_cfg, dict):
        code_cfg = {}

    def _normalize_string_entries(raw: Any) -> list[str]:
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        normalized: list[str] = []
        for item in raw:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    normalized.append(cleaned)
        return normalized

    result: dict[str, Any] = {
        "additional_authorized_imports": [],
        "additional_functions": {},
    }

    imports = _normalize_string_entries(code_cfg.get("additional_authorized_imports", []))
    if "*" in imports:
        result["additional_authorized_imports"] = ["*"]
    else:
        result["additional_authorized_imports"] = imports

    func_names = _normalize_string_entries(code_cfg.get("additional_functions", []))
    if "*" in func_names:
        result["additional_functions"] = {
            name: value for name, value in vars(builtins).items() if callable(value)
        }
        return result

    for name in func_names:
        try:
            result["additional_functions"][name] = getattr(builtins, name)
        except AttributeError as exc:
            raise AttributeError(
                f"Configuration error: '{name}' is not a valid Python built-in function."
            ) from exc
    return result
