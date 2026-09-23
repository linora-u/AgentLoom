"""AgentLoom public package surface."""

if __name__ != "agentloom":
    raise ImportError("Import AgentLoom as 'agentloom'; install the project first.")

__version__ = "1.0.1"

_LAZY_EXPORTS = {
    "C": ("agentloom.config", "C"),
    "get_config": ("agentloom.config", "get_config"),
    "get_default_toolsets": ("agentloom.config", "get_default_toolsets"),
    "get_model_config": ("agentloom.config", "get_model_config"),
    "ApplicationRunError": ("agentloom.app.run", "ApplicationRunError"),
    "ApplicationRunInterrupted": ("agentloom.app.run", "ApplicationRunInterrupted"),
    "ApplicationRunResult": ("agentloom.app.run", "ApplicationRunResult"),
    "RunEventSink": ("agentloom.app.run", "RunEventSink"),
    "RunInfo": ("agentloom.app.run", "RunInfo"),
    "RunLifecycleEvent": ("agentloom.app.run", "RunLifecycleEvent"),
    "RunPhase": ("agentloom.app.run", "RunPhase"),
    "RunRejectedEvent": ("agentloom.app.run", "RunRejectedEvent"),
    "RunRejection": ("agentloom.app.run", "RunRejection"),
    "execute_app": ("agentloom.app.runner", "execute_app"),
    "run_app": ("agentloom.app.runner", "run_app"),
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
