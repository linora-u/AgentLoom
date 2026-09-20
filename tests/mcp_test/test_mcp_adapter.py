from __future__ import annotations

import pytest
from agentloom.adapters.mcp.adapter import AgentLoomMCPAdapter, McpToolExecutionError
from agentloom.runtime.hooks import HookPlan, HookRun
from agentloom.runtime.tool_gateway import AgentLoomToolGateway
from agentloom.runtime.trace import ExplicitExecutionContext, bind_explicit_execution_context
from mcp.types import CallToolResult, TextContent, Tool


def _mcp_tool() -> Tool:
    return Tool(
        name="lookup",
        description="Look something up",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Query"}},
        },
    )


@pytest.mark.parametrize("original, visible", [("read-file", "read_file"), ("42-days", "_42_days"), ("class", "class_")])
def test_mcp_keeps_historical_visible_tool_names(original, visible):
    declaration = _mcp_tool().model_copy(update={"name": original})
    adapted = AgentLoomMCPAdapter().adapt(
        lambda arguments: CallToolResult(content=[TextContent(type="text", text=arguments["query"])]),
        declaration,
    )
    assert adapted.definition.name == visible
    assert adapted.forward(query="actual value") == "actual value"


def test_mcp_is_error_becomes_a_tool_execution_failure_with_all_text() -> None:
    adapter = AgentLoomMCPAdapter()
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
    adapter = AgentLoomMCPAdapter()
    adapted = adapter.adapt(
        lambda _arguments: CallToolResult(
            content=[TextContent(type="text", text='{"fallback": true}')],
            structuredContent={"items": [1, 2], "cursor": "next"},
        ),
        _mcp_tool(),
    )

    assert adapted.forward(query="agent state") == {"items": [1, 2], "cursor": "next"}


def test_mcp_error_keeps_kind_through_hook_and_canonical_settlement() -> None:
    adapted = AgentLoomMCPAdapter().adapt(
        lambda _arguments: CallToolResult(
            isError=True,
            content=[TextContent(type="text", text="database unavailable")],
        ),
        _mcp_tool(),
    )
    gateway = AgentLoomToolGateway.from_tools(
        [adapted],
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
        settled = gateway.invoke(
            call_id="mcp-call",
            tool_name=adapted.definition.name,
            arguments={"query": "agent state"},
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
