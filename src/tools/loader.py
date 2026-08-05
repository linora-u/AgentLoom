"""Resolve built-in tool implementation references on demand."""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from importlib import import_module
from typing import Any

from .catalog import get_tool_spec


@cache
def resolve_tool_function(tool_name: str) -> Callable[..., Any]:
    """Load one registered tool implementation and cache the callable."""

    spec = get_tool_spec(tool_name)
    reference = spec.implementation
    module = import_module(reference.module)
    implementation = getattr(module, reference.attribute, None)
    if not callable(implementation):
        raise RuntimeError(
            f"Tool '{spec.name}' implementation "
            f"'{reference.module}:{reference.attribute}' is not callable"
        )
    return implementation
