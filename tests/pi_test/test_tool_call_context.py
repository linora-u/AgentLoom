"""Tool-call identity and assistant batch through genuine PI execution."""

from dataclasses import asdict

import pytest
import yaml
from agentloom.app.application import Application
from agentloom.app.tool_factory import bind_tool_factory_bindings
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolBinding

from tests.pi_test.test_application import model_service, project


def create_probe(context):
    from agentloom.execution.tool_context import current_tool_call_context

    observations = context.bindings["observations"]

    def probe(fail=False):
        call = current_tool_call_context()
        observations.append(asdict(call))
        if fail:
            raise ValueError("Deliberate correctable tool error")
        return {"assistant_tools": list(call.assistant_tools)}

    schema = {"type": "object", "properties": {"fail": {"type": "boolean"}}, "additionalProperties": False}
    return ToolBinding(
        definition=ToolDefinition(name="probe", description="Inspect current call.", parameters=schema),
        inputs_schema=schema["properties"], forward=probe,
    )


def create_finish(context):
    from agentloom.execution.tool_context import current_tool_call_context

    def finish():
        call = current_tool_call_context()
        context.bindings["observations"].append(asdict(call))
        return "finished"

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    return ToolBinding(
        definition=ToolDefinition(name="finish", description="Finish.", parameters=schema),
        inputs_schema={}, forward=finish, terminal_on_success=True,
    )


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("trace_mode", ["full", "off"])
def test_current_tool_context_has_real_batch_and_resets_after_errors(tmp_path, failure, trace_mode):
    observations = []
    turns = [[("probe-1", "probe", {"fail": failure}), ("probe-2", "probe", {"fail": False})],
             [("finish-1", "finish", {})]]
    with model_service(turns=turns) as (url, requests):
        definition = project(tmp_path, url)
        system_path = tmp_path / "config/system.yaml"
        system = yaml.safe_load(system_path.read_text())
        system["runtime"] = {"trace": {"mode": trace_mode}}
        system_path.write_text(yaml.safe_dump(system))
        value = yaml.safe_load(definition.read_text())
        value["tools"] = [
            {"name": "probe", "factory": {"module": __name__, "function": "create_probe"}},
            {"name": "finish", "factory": {"module": __name__, "function": "create_finish"}},
        ]
        value["tool_metadata"] = {"probe": {"is_concurrency_safe": False}}
        definition.write_text(yaml.safe_dump(value))
        with bind_config(load_project_config(tmp_path)), Application.open(
            definition.parents[1], project_root=tmp_path, file_logging=False
        ) as app:
            with bind_tool_factory_bindings({"observations": observations}):
                result = app.worker("workflows/root.yaml").invoke(WorkItem("case", {}))
    assert result.status == "success", result.error
    assert [row["identity"]["call_id"] for row in observations] == ["probe-1", "probe-2", "finish-1"]
    assert [row["assistant_tools"] for row in observations] == [
        ("probe", "probe"), ("probe", "probe"), ("finish",),
    ]
    assert observations[0]["identity"]["native_parent_id"] == observations[1]["identity"]["native_parent_id"]
    assert observations[2]["identity"]["native_parent_id"] != observations[0]["identity"]["native_parent_id"]
    assert len(requests) == 2
    from agentloom.execution.tool_context import current_tool_call_context

    assert current_tool_call_context() is None
