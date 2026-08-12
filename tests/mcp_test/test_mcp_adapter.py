from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from src.mcp.adapter import AgentLoomSmolAgentsAdapter, McpToolExecutionError


def _mcp_tool() -> Tool:
    return Tool(
        name="lookup",
        description="Look something up",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Query"}},
        },
    )


def test_mcp_is_error_becomes_a_tool_execution_failure_with_all_text() -> None:
    adapter = AgentLoomSmolAgentsAdapter(structured_output=True)
    adapted = adapter.adapt(
        lambda _arguments: CallToolResult(
            isError=True,
            content=[
                TextContent(type="text", text="database unavailable"),
                TextContent(type="text", text="retry after reconnecting"),
            ],
        ),
        _mcp_tool(),
    )

    with pytest.raises(McpToolExecutionError) as captured:
        adapted.forward(query="agent state")

    assert str(captured.value) == "database unavailable\nretry after reconnecting"
    assert captured.value.kind == "mcp_error"
    assert captured.value.retryable is False


def test_mcp_structured_content_is_preserved_on_success() -> None:
    adapter = AgentLoomSmolAgentsAdapter(structured_output=True)
    adapted = adapter.adapt(
        lambda _arguments: CallToolResult(
            content=[TextContent(type="text", text='{"fallback": true}')],
            structuredContent={"items": [1, 2], "cursor": "next"},
        ),
        _mcp_tool(),
    )

    assert adapted.forward(query="agent state") == {"items": [1, 2], "cursor": "next"}
