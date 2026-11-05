"""Invocation-scoped YAML factories through the published PI SDK."""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml
from agentloom.app.application import Application
from agentloom.app.tool_factory import (
    bind_tool_factory_bindings,
    bind_tool_factory_context,
    current_tool_factory_context,
)
from agentloom.app.validation import AgentConfigNormalizer
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency.batch_models import WorkItem
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolBinding
from jsonschema import Draft202012Validator

from tests.pi_test.test_application import model_service, project


def create_save_tool(context):
    value = context.input["value"]
    schema = {
        "type": "object", "properties": {"value": {"type": "integer", "enum": [value]}},
        "required": ["value"], "additionalProperties": False,
    }
    host = context.bindings["host"]

    def save(value):
        if value < 0:
            raise OSError("injected filesystem failure")
        host.append(value)
        return {"value": value}

    return ToolBinding(
        definition=ToolDefinition(name="save_value", description="Save this fixture value.", parameters=schema),
        inputs_schema=schema["properties"], forward=save,
        input_validator=Draft202012Validator(schema).validate, terminal_on_success=True,
    )


def create_invalid_tool(context):
    if context.input["mode"] == "type":
        return object()
    return replace(create_save_tool(context), definition=ToolDefinition(
        name="wrong_name", description="Invalid factory fixture", parameters={"type": "object"},
    ))


def configure(path, factory="create_save_tool"):
    definition = yaml.safe_load(path.read_text())
    definition["input_schema"] = {
        "type": "object", "properties": {"value": {"type": "integer"}, "mode": {"type": "string"}},
        "required": ["value"], "additionalProperties": False,
    }
    definition["tools"] = [{
        "name": "save_value", "factory": {"module": __name__, "function": factory},
    }]
    path.write_text(yaml.safe_dump(definition))


def test_factory_schema_and_host_bindings_are_invocation_scoped(tmp_path):
    turns = [[(f"call-{i}", "save_value", {"value": i})] for i in [1, 2]]
    saved = []
    with model_service(turns=turns) as (url, requests):
        path = project(tmp_path, url)
        configure(path)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            with bind_tool_factory_bindings({"host": saved, "secret": "HOST-ONLY-SENTINEL"}):
                worker = app.worker("workflows/root.yaml")
                for value in [1, 2]:
                    outcome = worker.invoke(WorkItem(str(value), {"value": value}))
                    assert outcome.status == "success", outcome.error
                    assert outcome.output == {"value": value}
    assert saved == [1, 2] and len(requests) == 2
    for value, (_, request, _) in enumerate(requests, 1):
        assert len(request["tools"]) == 1
        schema = request["tools"][0]["function"]["parameters"]
        assert schema["properties"]["value"]["enum"] == [value]
        assert "HOST-ONLY-SENTINEL" not in str(request)
    with pytest.raises(RuntimeError, match="active Agent invocation"):
        current_tool_factory_context()


def test_factory_invalid_arguments_correct_in_same_session(tmp_path):
    saved = []
    with model_service(turns=[
        [("invalid", "save_value", {"value": 2})],
        [("corrected", "save_value", {"value": 1})],
    ]) as (url, requests):
        path = project(tmp_path, url)
        configure(path)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            with bind_tool_factory_bindings({"host": saved}):
                result = app.worker("workflows/root.yaml").invoke(WorkItem("correct", {"value": 1}))
    assert result.status == "success", result.error
    assert saved == [1] and len(requests) == 2
    assert any(m["role"] == "tool" and "1" in str(m["content"]) for m in requests[1][1]["messages"])


def test_factory_unexpected_exception_terminates_without_another_request(tmp_path):
    saved = []
    with model_service(turns=[[("failure", "save_value", {"value": -1})]]) as (url, requests):
        path = project(tmp_path, url)
        configure(path)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            with bind_tool_factory_bindings({"host": saved}):
                result = app.worker("workflows/root.yaml").invoke(WorkItem("fail", {"value": -1}))
    assert result.status == "failed"
    assert "injected filesystem failure" in result.error.message
    assert saved == [] and len(requests) == 1


@pytest.mark.parametrize("mode", ["type", "name"])
def test_invalid_factory_result_fails_before_model_request(tmp_path, mode):
    with model_service() as (url, requests):
        path = project(tmp_path, url)
        configure(path, "create_invalid_tool")
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            with bind_tool_factory_bindings({"host": []}):
                outcome = app.worker("workflows/root.yaml").invoke(WorkItem(mode, {"value": 1, "mode": mode}))
    assert outcome.status == "failed" and "Tool factory" in outcome.error.message
    assert requests == []


@pytest.mark.parametrize("entry", [
    {"name": "x", "factory": None},
    {"name": "x", "factory": {"module": "m"}},
    {"name": "x", "factory": {"module": "m", "function": ""}},
    {"name": "x", "factory": {"module": "m", "function": "f"}, "fixed_args": {}},
    {"name": "x", "factory": {"module": "m", "function": "f"}, "module": "other", "function": "f"},
])
def test_invalid_factory_declaration_rejected_before_import(entry):
    with pytest.raises(ValueError):
        AgentConfigNormalizer.validate_tools_config_entries([entry])


def test_factory_context_is_a_defensive_snapshot_and_resets_after_error():
    raw = {"nested": {"value": 1}}
    with bind_tool_factory_bindings({"host": "outer"}):
        with pytest.raises(RuntimeError, match="injected"), bind_tool_factory_context(raw, agent_name="fixture"):
            context = current_tool_factory_context()
            raw["nested"]["value"] = 2
            assert context.input["nested"]["value"] == 1
            extracted = context.input["nested"]
            extracted["value"] = 3
            assert context.input["nested"]["value"] == 1
            assert context.bindings["host"] == "outer"
            with bind_tool_factory_bindings({"host": "inner"}), bind_tool_factory_context({}, agent_name="inner"):
                assert current_tool_factory_context().bindings["host"] == "inner"
            assert current_tool_factory_context() is context
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError, match="active Agent invocation"):
        current_tool_factory_context()
