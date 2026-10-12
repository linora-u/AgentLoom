"""Close registered handles without importing an Agent backend.

Handles belong to an Application Run and one Agent instance. Backend callbacks
capture their concrete handles; cleanup never relies on a later trace binding.
"""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from weakref import WeakValueDictionary

from agentloom.execution import get_current_run_context
from agentloom.execution.trace import get_current_agent_id

RuntimeKey = tuple[str, str, str, str]
_lock = RLock()


@dataclass
class _ResourceHandle:
    close: Callable[[], None]
    lock: RLock = field(default_factory=RLock)
    closed: bool = False


_resources: dict[tuple[RuntimeKey, str, str], _ResourceHandle] = {}


@dataclass
class _OwnerScope:
    run_closed: bool = False
    closed_instances: set[str] = field(default_factory=set)


_scopes: WeakValueDictionary[RuntimeKey, _OwnerScope] = WeakValueDictionary()
_bound_scope: ContextVar[_OwnerScope | None] = ContextVar("resource_owner_scope", default=None)


def _scope(runtime_key: RuntimeKey) -> _OwnerScope:
    # Caller holds _lock. Context copies retain this state for in-flight callbacks;
    # the weak map releases completed Runs once all execution contexts are gone.
    state = _scopes.get(runtime_key)
    if state is None:
        state = _OwnerScope()
        _scopes[runtime_key] = state
    return state


@contextmanager
def bind_resource_scope(runtime_key: RuntimeKey):
    with _lock:
        state = _scope(runtime_key)
    token = _bound_scope.set(state)
    try:
        yield
    finally:
        _bound_scope.reset(token)


def register_resource(key: str, close: Callable[[], None], *, instance_id: str | None = None) -> None:
    """Register a concrete handle; no bound Run means caller-owned lifetime."""
    context = get_current_run_context()
    if context is None:
        return
    owner = instance_id if instance_id is not None else get_current_agent_id() or ""
    resource_key = (context.runtime_key, owner, key)
    with _lock:
        state = _scope(context.runtime_key)
        cancelled = state.run_closed or owner in state.closed_instances
        handle = _resources.setdefault(resource_key, _ResourceHandle(close))
    if cancelled:
        _close_handle(resource_key, handle)


def _close_handle(resource_key: tuple[RuntimeKey, str, str], handle: _ResourceHandle) -> None:
    # Leave a failed handle registered under its original owner. The final Run
    # cleanup may retry it, and concurrent cleanup cannot call it twice at once.
    with handle.lock:
        if not handle.closed:
            handle.close()
            handle.closed = True
        with _lock:
            if _resources.get(resource_key) is handle:
                del _resources[resource_key]


def retain_failed_resource(key: str, close: Callable[[], None], *, instance_id: str) -> None:
    """Retain an already-failed close for the original Run's finalization retry.

    Unlike registering new work, this never calls the failed callback during
    registration, even when instance cleanup already marked its owner closed.
    """
    context = get_current_run_context()
    if context is None:
        return
    with _lock:
        _resources.setdefault((context.runtime_key, instance_id, key), _ResourceHandle(close))


def _close(instance_id: str | None) -> None:
    context = get_current_run_context()
    if context is None:
        return
    with _lock:
        state = _scope(context.runtime_key)
        if instance_id is None:
            state.run_closed = True
        else:
            state.closed_instances.add(instance_id)
        keys = [
            key
            for key in _resources
            if key[0] == context.runtime_key and (instance_id is None or key[1] == instance_id)
        ]
        handles = [(key, _resources[key]) for key in keys]
    errors = []
    for resource_key, handle in reversed(handles):
        try:
            _close_handle(resource_key, handle)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup("Execution resource cleanup failed", errors)


def close_instance_resources(instance_id: str) -> None:
    """Close only this instance within the bound Run; safe to repeat."""
    _close(instance_id)


def close_run_resources() -> None:
    """Application finalization fallback after its Agent invocations finish."""
    _close(None)


def unregister_resource(key: str, *, instance_id: str | None = None) -> None:
    """Forget an already closed handle in its original execution context."""
    context = get_current_run_context()
    if context is None:
        return
    owner = instance_id if instance_id is not None else get_current_agent_id() or ""
    with _lock:
        _resources.pop((context.runtime_key, owner, key), None)
