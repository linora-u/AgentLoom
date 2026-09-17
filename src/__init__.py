"""
AI Agents package.
"""

from src._compat import install_legacy_imports

install_legacy_imports()

from src.encoding.terminal import configure_terminal_encoding

configure_terminal_encoding()

__version__ = "1.0.1"

_LAZY_EXPORTS = {
    "C": ("src.configuration", "C"),
    "get_config": ("src.configuration", "get_config"),
    "get_default_toolsets": ("src.configuration", "get_default_toolsets"),
    "get_code_agent_config": ("src.configuration", "get_code_agent_config"),
    "get_model_config": ("src.configuration", "get_model_config"),
    "ApplicationRunError": ("src.application.run", "ApplicationRunError"),
    "ApplicationRunBudgetLimited": (
        "src.application.run",
        "ApplicationRunBudgetLimited",
    ),
    "ApplicationRunInterrupted": ("src.application.run", "ApplicationRunInterrupted"),
    "ApplicationRunResult": ("src.application.run", "ApplicationRunResult"),
    "RunEventSink": ("src.application.run", "RunEventSink"),
    "RunInfo": ("src.application.run", "RunInfo"),
    "RunLifecycleEvent": ("src.application.run", "RunLifecycleEvent"),
    "RunPhase": ("src.application.run", "RunPhase"),
    "RunRejectedEvent": ("src.application.run", "RunRejectedEvent"),
    "RunRejection": ("src.application.run", "RunRejection"),
    "execute_app": ("src.application.runner", "execute_app"),
    "run_app": ("src.application.runner", "run_app"),
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
    "ApplicationRunError",
    "ApplicationRunBudgetLimited",
    "ApplicationRunInterrupted",
    "ApplicationRunResult",
    "RunEventSink",
    "RunInfo",
    "RunLifecycleEvent",
    "RunPhase",
    "RunRejectedEvent",
    "RunRejection",
    "execute_app",
    "run_app",
]
