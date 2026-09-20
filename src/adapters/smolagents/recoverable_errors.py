"""Typed smolagents errors that represent completed model-feedback turns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from smolagents.agents import (
    AgentError,
    AgentExecutionError,
    AgentParsingError,
    AgentToolCallError,
    AgentToolExecutionError,
)

_RECOVERABLE_ERROR_TYPES = (
    AgentParsingError,
    AgentExecutionError,
    AgentToolCallError,
    AgentToolExecutionError,
)
_RECOVERABLE_ERROR_TYPES_BY_NAME = {
    error_type.__name__: error_type
    for error_type in _RECOVERABLE_ERROR_TYPES
}


class _CheckpointErrorLogger:
    def log_error(self, _message: str) -> None:
        return None


def is_recoverable_agent_error(error: Any) -> bool:
    """Return whether an exact smolagents error type is model-correctable."""

    return type(error) in _RECOVERABLE_ERROR_TYPES


def rebuild_recoverable_agent_error(raw: Any) -> AgentError | None:
    """Restore one allowlisted model-correctable error from checkpoint data."""

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("smolagents action error must be an object")
    error_type = _RECOVERABLE_ERROR_TYPES_BY_NAME.get(raw.get("type"))
    if error_type is None:
        raise ValueError(
            f"unsupported resumable smolagents error type: {raw.get('type')!r}"
        )
    message = raw.get("message")
    if not isinstance(message, str) or not message:
        raise ValueError("smolagents action error message must be non-empty")
    return error_type(message, _CheckpointErrorLogger())
