"""Work identity stays host-only while native PI maps equal model inputs."""

import pytest
import yaml
from agentloom.app.application import Application
from agentloom.app.tool_factory import bind_tool_factory_bindings, current_tool_factory_context
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolBinding

from tests.pi_test.test_application import model_service, project


def create_identity_tool(context):
    if context.item_id == "rejected":
        raise ValueError("Explicitly rejected host work identity")
    target = context.bindings["targets"][context.item_id]
    schema = {"type": "object", "properties": {"value": {"type": "integer", "enum": [7]}},
              "required": ["value"], "additionalProperties": False}

    def save(value):
        target.append(value)
        return {"saved": context.item_id, "value": value}

    return ToolBinding(
        definition=ToolDefinition(name="save_value", parameters=schema, description="Save the assigned value."),
        inputs_schema=schema["properties"], forward=save, terminal_on_success=True,
    )


def test_equal_inputs_use_distinct_host_work_bindings_through_native_pi_map(tmp_path):
    identities = [f"private-task-{index}" for index in range(5)]
    targets = {identity: [] for identity in identities}
    turns = [[(f"save-{index}", "save_value", {"value": 7})] for index in range(5)]
    with model_service(turns=turns) as (url, requests):
        definition = project(tmp_path, url)
        value = yaml.safe_load(definition.read_text())
        value["input_schema"] = {"type": "object", "properties": {"value": {"type": "integer"}},
                                 "required": ["value"], "additionalProperties": False}
        value["tools"] = [{"name": "save_value", "factory": {
            "module": __name__, "function": "create_identity_tool"}}]
        definition.write_text(yaml.safe_dump(value))
        with bind_config(load_project_config(tmp_path)), Application.open(
                definition.parents[1], project_root=tmp_path) as app:
            with bind_tool_factory_bindings({"targets": targets}):
                result = app.worker("workflows/root.yaml").map(
                    [WorkItem(identity, {"value": 7}) for identity in identities], concurrency=5)
        assert all(outcome.status == "success" for outcome in result.outcomes), result.to_dict()
    assert [outcome.output for outcome in result.outcomes] == [
        {"saved": identity, "value": 7} for identity in identities]
    assert targets == {identity: [7] for identity in identities}
    assert len(requests) == 5
    for _, request, _ in requests:
        assert not any(identity in str(request) for identity in identities)
    with pytest.raises(RuntimeError, match="active Agent invocation"):
        current_tool_factory_context()


def test_single_work_identity_and_nested_factory_scope_restore_after_error(tmp_path):
    from agentloom.app.tool_factory import bind_tool_factory_context, bind_tool_factory_work_item

    with bind_tool_factory_work_item("outer"), bind_tool_factory_context({}, agent_name="fixture"):
        outer = current_tool_factory_context()
        assert outer.item_id == "outer"
        with pytest.raises(ValueError), bind_tool_factory_work_item("inner"), bind_tool_factory_context(
                {}, agent_name="fixture"):
            assert current_tool_factory_context().item_id == "inner"
            raise ValueError("Explicit nested factory failure")
        assert current_tool_factory_context() is outer
    with bind_tool_factory_context({}, agent_name="unbound"):
        assert current_tool_factory_context().item_id is None
