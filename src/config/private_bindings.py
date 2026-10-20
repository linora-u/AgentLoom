"""Private Harness capability values, carried only by the trusted child pipe.

The resolver is inert during ordinary/static configuration reads. An active
Harness binding fails on an unresolved reference instead of sending a marker
as an API credential or reopening live source configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

_VALUES: ContextVar[Mapping[str, Any] | None] = ContextVar("agentloom_harness_private_capabilities", default=None)
_PREFIX = "harness-credential-ref:"


@contextmanager
def bind_harness_credentials(values: Mapping[str, Any]):
    token = _VALUES.set(deepcopy(dict(values)))
    try:
        yield
    finally:
        _VALUES.reset(token)


def resolve_bound_credentials(value: Any) -> Any:
    bindings = _VALUES.get()
    if bindings is None:
        return value
    if isinstance(value, str) and value.startswith(_PREFIX):
        reference = value[len(_PREFIX):]
        if reference not in bindings:
            raise ValueError("A frozen Harness capability reference has no trusted binding")
        return deepcopy(bindings[reference])
    if isinstance(value, Mapping):
        return {key: resolve_bound_credentials(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_bound_credentials(item) for item in value]
    return value
