"""AgentLoom MCP adapter with protocol-correct error handling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool
from mcpadapt.smolagents_adapter import SmolAgentsAdapter


class McpToolExecutionError(RuntimeError):
    """An MCP server returned a terminal tool error result."""

    kind = "mcp_error"
    stage = "tool_execution"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _mcp_error_text(result: CallToolResult) -> str:
    text_parts = [item.text for item in result.content if isinstance(item, TextContent) and item.text]
    if text_parts:
        return "\n".join(text_parts)
    if result.structuredContent is not None:
        return str(result.structuredContent)
    return "MCP tool returned an error without details."


class AgentLoomSmolAgentsAdapter(SmolAgentsAdapter):
    """Preserve structured MCP output and turn ``isError`` into an exception."""

    def adapt(
        self,
        func: Callable[[dict | None], CallToolResult],
        mcp_tool: Tool,
    ) -> Any:
        def checked(arguments: dict | None) -> CallToolResult:
            result = func(arguments)
            if result.isError:
                raise McpToolExecutionError(_mcp_error_text(result))
            return result

        return super().adapt(checked, mcp_tool)
