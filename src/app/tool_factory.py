"""Invocation-scoped tool factories for YAML-defined Agents."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from agentloom.execution.native_tools import JsonObject

_BINDINGS: ContextVar[Mapping[str, Any]] = ContextVar("agentloom_tool_factory_bindings", default=MappingProxyType({}))
_CURRENT: ContextVar[ToolFactoryContext | None] = ContextVar("agentloom_tool_factory_context", default=None)
_WORK_ITEM: ContextVar[str | None] = ContextVar("agentloom_tool_factory_work_item", default=None)


@dataclass(frozen=True)
class ToolFactoryContext:
    """Actual Worker input plus explicit, host-only dependencies.

    ``input`` is a defensive JSON snapshot. ``bindings`` never enters prompts,
    YAML, model arguments or persistence; use it for output/storage dependencies.
    ``item_id`` identifies the current WorkItem only to trusted factories, even
    when separate tasks have identical model-visible inputs.
    """

    input: JsonObject
    bindings: Mapping[str, Any]
    agent_name: str
    item_id: str | None = None


@contextmanager
def bind_tool_factory_work_item(item_id: str):
    """Bind trusted Worker identity independently from model-visible JSON."""
    token = _WORK_ITEM.set(item_id)
    try:
        yield
    finally:
        _WORK_ITEM.reset(token)


@contextmanager
def bind_tool_factory_bindings(bindings: Mapping[str, Any]):
    token = _BINDINGS.set(MappingProxyType(dict(bindings)))
    try:
        yield
    finally:
        _BINDINGS.reset(token)


@contextmanager
def bind_tool_factory_context(input_data: Mapping | None, *, agent_name: str):
    context = ToolFactoryContext(JsonObject(input_data or {}), _BINDINGS.get(), agent_name, _WORK_ITEM.get())
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def current_tool_factory_context() -> ToolFactoryContext:
    context = get_current_tool_factory_context()
    if context is None:
        raise RuntimeError("YAML tool factories require an active Agent invocation")
    return context


def get_current_tool_factory_context() -> ToolFactoryContext | None:
    """Return creation Context without creating a new invocation scope."""
    return _CURRENT.get()
