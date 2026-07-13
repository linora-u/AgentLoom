"""
AI Agents package.
"""

from src.encoding.terminal import configure_terminal_encoding

configure_terminal_encoding()

__version__ = "0.1.0"

_LAZY_EXPORTS = {
    "C": ("src.lib.config", "C"),
    "get_config": ("src.lib.config", "get_config"),
    "get_default_toolsets": ("src.lib.config", "get_default_toolsets"),
    "get_code_agent_config": ("src.lib.config", "get_code_agent_config"),
    "get_model_config": ("src.lib.config", "get_model_config"),
    "run_app": ("src.runner", "run_app"),
}


def __getattr__(name: str):
    """Preserve the public package API without loading the agent runtime eagerly."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})

__all__ = [
    "__version__",
    "C",
    "get_config",
    "get_default_toolsets",
    "get_code_agent_config",
    "get_model_config",
    "run_app",
]
