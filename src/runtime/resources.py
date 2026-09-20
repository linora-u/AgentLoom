"""Close registered handles without importing an Agent backend.

Handles belong to an Application Run and one Agent instance. Backend callbacks
capture their concrete handles; cleanup never relies on a later trace binding.
"""

from collections.abc import Callable
from threading import RLock

from agentloom.runtime import get_current_run_context
from agentloom.runtime.trace import get_current_agent_id

RuntimeKey = tuple[str, str, str, str]
_lock = RLock()
_resources: dict[tuple[RuntimeKey, str, str], Callable[[], None]] = {}


def register_resource(key: str, close: Callable[[], None], *, instance_id: str | None = None) -> None:
    """Register a concrete handle; no bound Run means caller-owned lifetime."""
    context = get_current_run_context()
    if context is None:
        return
    owner = instance_id if instance_id is not None else get_current_agent_id() or ""
    with _lock:
        _resources.setdefault((context.runtime_key, owner, key), close)


def _close(instance_id: str | None) -> None:
    context = get_current_run_context()
    if context is None:
        return
    with _lock:
        keys = [
            key
            for key in _resources
            if key[0] == context.runtime_key and (instance_id is None or key[1] == instance_id)
        ]
        callbacks = [_resources.pop(key) for key in keys]
    errors = []
    for callback in reversed(callbacks):
        try:
            callback()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Execution resource cleanup failed", errors)


def close_instance_resources(instance_id: str) -> None:
    """Close only this instance within the bound Run; safe to repeat."""
    _close(instance_id)


def close_run_resources() -> None:
    """Application finalization fallback after its Agent invocations finish."""
    _close(None)
