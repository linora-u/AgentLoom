"""Trusted execution Context through the native Pi worker map."""

from dataclasses import replace
from threading import RLock

import yaml
from agentloom.app.application import Application
from agentloom.app.tool_factory import bind_tool_factory_bindings
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.tool_context import ToolContext, current_tool_context
from agentloom.execution.tool_gateway import bind_tool

from tests.pi_test.test_application import model_service, project


def create_context_save(_factory):
    def save_value(value: int, *, ctx: ToolContext):
        assert current_tool_context() is ctx
        assert ctx.identity.call_id == ctx.call_id
        assert ctx.assistant_tools == ("save_value",)
        with ctx.bindings["lock"]:
            ctx.bindings["targets"][ctx.item_id].append(value)
            ctx.bindings["observed"][ctx.item_id] = ctx
        return {"saved": ctx.item_id, "value": value}

    binding = bind_tool(save_value)
    return replace(binding, terminal_on_success=True)


def test_equal_inputs_inject_distinct_work_context_after_factory_scope_ends(tmp_path):
    identities = [f"host-only-{index}" for index in range(5)]
    targets = {identity: [] for identity in identities}
    observed = {}
    dependencies = {"targets": targets, "observed": observed, "lock": RLock()}
    turns = [[(f"save-{index}", "save_value", {"value": 7})] for index in range(5)]
    with model_service(turns=turns) as (url, requests):
        definition = project(tmp_path, url)
        value = yaml.safe_load(definition.read_text())
        value["tools"] = [{"name": "save_value", "factory": {
            "module": __name__, "function": "create_context_save",
        }}]
        definition.write_text(yaml.safe_dump(value))
        with bind_config(load_project_config(tmp_path)), Application.open(
            definition.parents[1], project_root=tmp_path
        ) as app:
            with bind_tool_factory_bindings(dependencies):
                result = app.worker("workflows/root.yaml").map(
                    [WorkItem(identity, {}) for identity in identities], concurrency=5,
                )
    assert all(outcome.status == "success" for outcome in result.outcomes), result.to_dict()
    assert targets == {identity: [7] for identity in identities}
    assert set(observed) == set(identities)
    assert len({id(context) for context in observed.values()}) == 5
    assert all(context.bindings["lock"] is dependencies["lock"] for context in observed.values())
    for _, request, _ in requests:
        assert not any(identity in str(request) for identity in identities)
        tool = next(tool for tool in request["tools"] if tool["function"]["name"] == "save_value")
        assert "ctx" not in tool["function"]["parameters"]["properties"]
    assert current_tool_context() is None
