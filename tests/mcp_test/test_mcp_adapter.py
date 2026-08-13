from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from src.lib.smolagents.hooks import HookPlan, HookRun
from src.lib.smolagents.hooks.tool_shim import inject_hooks
from src.lib.smolagents.tool_protocol import settle_tool_call
from src.mcp.adapter import AgentLoomSmolAgentsAdapter, McpToolExecutionError
from src.trace import ExplicitExecutionContext, bind_explicit_execution_context


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


def test_mcp_error_keeps_kind_through_hook_and_canonical_settlement() -> None:
    adapted = AgentLoomSmolAgentsAdapter(structured_output=True).adapt(
        lambda _arguments: CallToolResult(
            isError=True,
            content=[TextContent(type="text", text="database unavailable")],
        ),
        _mcp_tool(),
    )
    run = HookRun(HookPlan(), local_run_id="mcp-local", root_run_id="mcp-root")
    execution = ExplicitExecutionContext(
        task_id="mcp-task",
        sub_task_id=None,
        agent_id="mcp-agent",
        agent_name="agent",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id="mcp-root",
        local_run_id="mcp-local",
    )

    with bind_explicit_execution_context(execution):
        settled = settle_tool_call(
            inject_hooks(adapted),
            {"query": "agent state"},
            call_id="mcp-call",
            sanitize_inputs_outputs=True,
        )

    assert settled.status == "error"
    assert settled.error is not None
    assert settled.error.kind == "mcp_error"
    assert settled.error.stage == "tool_execution"
    assert settled.model_content() == (
        '{"ok":false,"status":"error","error":'
        '{"kind":"mcp_error","message":"database unavailable",'
        '"retryable":false,"stage":"tool_execution"}}'
    )
    traced = run.tool_outcomes_snapshot()
    assert len(traced) == 1
    assert traced[0].status == "error"
    assert traced[0].error is not None
    assert traced[0].error.kind == "mcp_error"
    assert traced[0].error.retryable is False
