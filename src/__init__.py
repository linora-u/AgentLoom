"""
AI Agents package.
"""

from src.encoding.terminal import configure_terminal_encoding

configure_terminal_encoding()

__version__ = "1.0.1"

_LAZY_EXPORTS = {
    "C": ("src.lib.config", "C"),
    "get_config": ("src.lib.config", "get_config"),
    "get_default_toolsets": ("src.lib.config", "get_default_toolsets"),
    "get_code_agent_config": ("src.lib.config", "get_code_agent_config"),
    "get_model_config": ("src.lib.config", "get_model_config"),
    "ApplicationRunError": ("src.application_run", "ApplicationRunError"),
    "ApplicationRunBudgetLimited": (
        "src.application_run",
        "ApplicationRunBudgetLimited",
    ),
    "ApplicationRunInterrupted": ("src.application_run", "ApplicationRunInterrupted"),
    "ApplicationRunResult": ("src.application_run", "ApplicationRunResult"),
    "RunEventSink": ("src.application_run", "RunEventSink"),
    "RunInfo": ("src.application_run", "RunInfo"),
    "RunLifecycleEvent": ("src.application_run", "RunLifecycleEvent"),
    "RunPhase": ("src.application_run", "RunPhase"),
    "RunRejectedEvent": ("src.application_run", "RunRejectedEvent"),
    "RunRejection": ("src.application_run", "RunRejection"),
    "execute_app": ("src.runner", "execute_app"),
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
