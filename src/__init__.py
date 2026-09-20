"""
AI Agents package.
"""

import os

if __name__ != "agentloom":
    raise ImportError("Import AgentLoom as 'agentloom'; install the project first.")

# LiteLLM fetches its model-price catalog during import unless this is set.
# AgentLoom imports provider adapters from offline validation, CLI inspection,
# and checkpoint subprocesses, so imports must not depend on external network
# availability. Preserve an explicit user override.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from agentloom.encoding.terminal import configure_terminal_encoding

configure_terminal_encoding()

__version__ = "1.0.1"

_LAZY_EXPORTS = {
    "C": ("agentloom.configuration", "C"),
    "get_config": ("agentloom.configuration", "get_config"),
    "get_default_toolsets": ("agentloom.configuration", "get_default_toolsets"),
    "get_model_config": ("agentloom.configuration", "get_model_config"),
    "ApplicationRunError": ("agentloom.application.run", "ApplicationRunError"),
    "ApplicationRunInterrupted": ("agentloom.application.run", "ApplicationRunInterrupted"),
    "ApplicationRunResult": ("agentloom.application.run", "ApplicationRunResult"),
    "RunEventSink": ("agentloom.application.run", "RunEventSink"),
    "RunInfo": ("agentloom.application.run", "RunInfo"),
    "RunLifecycleEvent": ("agentloom.application.run", "RunLifecycleEvent"),
    "RunPhase": ("agentloom.application.run", "RunPhase"),
    "RunRejectedEvent": ("agentloom.application.run", "RunRejectedEvent"),
    "RunRejection": ("agentloom.application.run", "RunRejection"),
    "execute_app": ("agentloom.application.runner", "execute_app"),
    "run_app": ("agentloom.application.runner", "run_app"),
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
    "get_model_config",
    "ApplicationRunError",
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
