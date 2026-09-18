"""Runtime-neutral Tool discovery, execution, and lifecycle contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.tool_protocol import ToolCallRecord


@runtime_checkable
class ToolGateway(Protocol):
    """The only Tool execution seam exposed to an Agent runtime adapter.

    ``definitions`` is an immutable snapshot of the Tools visible to the
    runtime.  ``invoke`` must preserve the caller-supplied, non-empty
    ``call_id`` in its terminal :class:`ToolCallRecord`; implementations do
    not generate or replace provider call IDs.  ``close`` releases any
    Tool-owned resources and must be safe to call more than once.
    """

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]: ...

    def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolCallRecord: ...

    def close(self) -> None: ...
