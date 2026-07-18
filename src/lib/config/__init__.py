"""Config package public exports."""

_LAZY_EXPORTS = {
    "C": (".config", "C"),
    "ConfigLayerSnapshot": (".config", "ConfigLayerSnapshot"),
    "EffectiveAgentConfigSnapshot": (".config", "EffectiveAgentConfigSnapshot"),
    "build_effective_agent_config": (".config", "build_effective_agent_config"),
    "build_effective_agent_config_snapshot": (
        ".config",
        "build_effective_agent_config_snapshot",
    ),
    "get_code_agent_config": (".config", "get_code_agent_config"),
    "get_config": (".config", "get_config"),
    "get_default_toolsets": (".config", "get_default_toolsets"),
    "get_model_config": (".config", "get_model_config"),
    "RootSettings": (".config_validation", "RootSettings"),
    "LoggingSettings": (".config_validation", "LoggingSettings"),
    "RuntimeSettings": (".config_validation", "RuntimeSettings"),
    "normalize_tool_access_control_section": (
        ".config_validation",
        "normalize_tool_access_control_section",
    ),
    "raise_project_key_error": (".config_validation", "raise_project_key_error"),
    "validate_system_snapshot": (".config_validation", "validate_system_snapshot"),
    "LayeredConfigBuilder": (".layered_builder", "LayeredConfigBuilder"),
    "OverlaySpec": (".layered_builder", "OverlaySpec"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})

__all__ = [
    "C",
    "ConfigLayerSnapshot",
    "EffectiveAgentConfigSnapshot",
    "LayeredConfigBuilder",
    "LoggingSettings",
    "OverlaySpec",
    "RootSettings",
    "RuntimeSettings",
    "build_effective_agent_config",
    "build_effective_agent_config_snapshot",
    "get_code_agent_config",
    "get_config",
    "get_default_toolsets",
    "get_model_config",
    "normalize_tool_access_control_section",
    "raise_project_key_error",
    "validate_system_snapshot",
]
