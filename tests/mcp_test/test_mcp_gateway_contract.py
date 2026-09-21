"""Full MCP schemas are enforced at the public final-decode boundary."""

from dataclasses import replace

import pytest
from agentloom.integrations.mcp.adapter import AgentLoomMCPAdapter
from agentloom.runtime.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from agentloom.runtime.tool_gateway import AgentLoomToolGateway
from agentloom.runtime.trace import bind_explicit_execution_context, capture_explicit_execution_context
from mcp.types import CallToolResult, Tool

SCHEMA = {
    "type": "object", "properties": {"query": {"$ref": "#/$defs/query"}},
    "$defs": {"query": {"type": "string", "minLength": 3}},
    "required": ["query"], "additionalProperties": {"type": "integer"},
}


@pytest.mark.parametrize("arguments", [{"query": "x"}, {"query": 1}, {"query": "valid", "count": "bad"}])
def test_invalid_mcp_schema_blocks_before_guard_history_and_recording(arguments, monkeypatch):
    phases = []
    calls = []
    binding = AgentLoomMCPAdapter().adapt(
        lambda value: calls.append(value) or CallToolResult(content=[], structuredContent=value),
        Tool(name="lookup", inputSchema=SCHEMA),
    )
    for target in (
        "agentloom.runtime.hooks.path_validators.enforce_core_tool_guard",
        "agentloom.runtime.checkpoint.file_history_hook.record_active_file_history",
        "agentloom.runtime.tool_gateway._observe_final_tool_input",
    ):
        monkeypatch.setattr(target, lambda *args, **kwargs: phases.append("unexpected") or HookResult())
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    with bind_explicit_execution_context(replace(capture_explicit_execution_context(), hook_run=run)):
        result = AgentLoomToolGateway([binding]).invoke(call_id="invalid", tool_name="lookup", arguments=arguments)
    assert result.status == "blocked", result.model_content()
    assert result.error.stage == "final_decode"
    assert phases == []
    assert calls == []


def test_mcp_hook_repairs_input_before_full_validation_and_allows_declared_extra_properties():
    calls = []
    binding = AgentLoomMCPAdapter().adapt(
        lambda value: calls.append(value) or CallToolResult(content=[], structuredContent=value),
        Tool(name="lookup", inputSchema=SCHEMA),
    )
    run = HookRun(HookPlan([HookHandler(
        HookEvent.PRE_TOOL_USE, "lookup",
        lambda context: HookResult(decision="modify", modified_input={"query": "repaired", "count": 2}),
    )]), local_run_id="local", root_run_id="root")
    with bind_explicit_execution_context(replace(capture_explicit_execution_context(), hook_run=run, agent_config={})):
        result = AgentLoomToolGateway([binding]).invoke(call_id="repaired", tool_name="lookup", arguments={"query": "x"})
    assert result.status == "completed", result.model_content()
    assert calls == [{"query": "repaired", "count": 2}]


@pytest.mark.parametrize("property_schema", [
    {"$ref": "#/$defs/query"},
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
    {"type": ["string", "null"]},
    True,
])
def test_smol_projection_accepts_mcp_schema_without_changing_canonical_definition(property_schema):
    pytest.importorskip("smolagents")
    from agentloom.runtimes.smolagents.model_turn_bridge import _tool_definition
    from agentloom.runtimes.smolagents.tool_proxy import SmolagentsToolGatewayProxy

    schema = {**SCHEMA, "properties": {"query": property_schema}}
    binding = AgentLoomMCPAdapter().adapt(lambda _: CallToolResult(content=[]), Tool(name="lookup", inputSchema=schema))
    proxy = SmolagentsToolGatewayProxy(gateway=AgentLoomToolGateway([binding]), definition=binding.definition)
    assert _tool_definition(proxy).parameters == schema
