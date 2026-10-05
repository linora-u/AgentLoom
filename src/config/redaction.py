"""Redaction policy fixed for an invocation and inherited by its Workers."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_RUN_REDACTION: ContextVar[bool | None] = ContextVar("agentloom_redaction", default=None)


def redaction_enabled(config: Mapping[str, Any] | None = None) -> bool:
    if config is None:
        pinned = _RUN_REDACTION.get()
        if pinned is not None:
            return pinned
        from agentloom.config.config import get_config

        config = get_config().raw
    section = config.get("redaction", {})
    if not isinstance(section, Mapping):
        raise ValueError("redaction must be a mapping")
    enabled = section.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("redaction.enabled must be a boolean")
    return enabled


@contextmanager
def bind_redaction_policy(
    config: Mapping[str, Any], *, inherit: bool = False,
) -> Iterator[bool]:
    pinned = _RUN_REDACTION.get()
    enabled = pinned if inherit and pinned is not None else redaction_enabled(config)
    token = _RUN_REDACTION.set(enabled)
    try:
        yield enabled
    finally:
        _RUN_REDACTION.reset(token)
