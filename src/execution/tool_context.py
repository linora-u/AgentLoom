"""Read-only identity of the native tool call currently being dispatched."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agentloom.execution.native_tools import NativeCallIdentity


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Host-only dependencies and invocation facts for one Tool execution.

    Annotate a keyword-capable Tool parameter with this type to receive it
    without adding a model-visible argument. Bindings are a shallow read-only
    snapshot: session objects keep their identity and are never serialized.
    """

    call_id: str
    tool_name: str
    item_id: str | None = None
    agent_name: str | None = None
    bindings: Mapping[str, Any] = field(default_factory=dict, repr=False)
    identity: NativeCallIdentity | None = None
    assistant_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


_EXECUTION: ContextVar[ToolContext | None] = ContextVar("agentloom_tool_context", default=None)


def current_tool_context() -> ToolContext | None:
    """Return trusted execution Context, or None outside a Tool callback."""
    return _EXECUTION.get()


@contextmanager
def bind_tool_context(context: ToolContext):
    token = _EXECUTION.set(context)
    try:
        yield context
    finally:
        _EXECUTION.reset(token)


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    """Runtime-owned call identity and ordered names in its assistant batch."""

    identity: NativeCallIdentity
    assistant_tools: tuple[str, ...]


_CURRENT: ContextVar[ToolCallContext | None] = ContextVar("agentloom_tool_call_context", default=None)
_DISPATCH_OWNER: ContextVar[object | None] = ContextVar("agentloom_tool_dispatch_owner", default=None)


def current_tool_call_context() -> ToolCallContext | None:
    """Return current native dispatch facts, or None outside native dispatch."""
    return _CURRENT.get()


def claim_tool_call_context(call_id: str, owner: object) -> ToolCallContext | None:
    """Associate a prepared Gateway call with this native dispatch scope.

    Direct nested Gateway calls do not create native dispatches, even when
    they reuse the enclosing call ID. A nested runtime dispatch binds a fresh
    scope through ``bind_tool_call_context`` instead.
    """
    context = _CURRENT.get()
    if context is None or context.identity.call_id != call_id:
        return None
    assigned = _DISPATCH_OWNER.get()
    if assigned is not None and assigned is not owner:
        return None
    _DISPATCH_OWNER.set(owner)
    return context


@contextmanager
def bind_tool_call_context(context: ToolCallContext):
    token = _CURRENT.set(context)
    owner = _DISPATCH_OWNER.set(None)
    try:
        yield context
    finally:
        _DISPATCH_OWNER.reset(owner)
        _CURRENT.reset(token)
