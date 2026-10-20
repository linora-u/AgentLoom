"""Actual multi-level delegation observed through the public Application runner."""

import pytest
import yaml

from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config

from tests.application_test.mixed_runtime_support import finish, model_service, project, tool_messages, write_yaml


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_worker_can_delegate_to_its_pinned_subagent(tmp_path, runtime):
    def program(request):
        visible = "\n".join(str(message.get("content")) for message in request["messages"])
        if "LEAF-ROLE-136" in visible:
            return finish(request, "LEAF-EVIDENCE-136")
        if request["model"] == "worker":
            if not tool_messages(request):
                names = {tool["function"]["name"] for tool in request.get("tools", [])}
                assert "consult_leaf" in names, "Worker YAML declared a subagent that was not actually bound"
                return [("call-leaf-136", "consult_leaf", {"query": "Verify the leaf evidence."})]
            assert "LEAF-EVIDENCE-136" in tool_messages(request)[-1]["content"]
            return finish(request, "MIDDLE-EVIDENCE-136")
        if not tool_messages(request):
            return [("call-middle-136", "inspect_note", {"query": "Ask the leaf to verify."})]
        assert "MIDDLE-EVIDENCE-136" in tool_messages(request)[-1]["content"]
        return finish(request, "ROOT-EVIDENCE-136")

    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        middle = definition.parent / "worker_agents/inspect.yaml"
        config = yaml.safe_load(middle.read_text())
        config["tools"] = []
        config["worker_agents"] = [{"path": "workflows/worker_agents/leaf.yaml"}]
        write_yaml(middle, config)
        write_yaml(middle.with_name("leaf.yaml"), {
            "name": "consult_leaf",
            "agent_runtime": runtime,
            "model_type": "worker",
            "description": "Verify the original evidence independently.",
            "task": "LEAF-ROLE-136: return the independent leaf evidence.",
            "tools": [],
            "toolsets": [],
            "input_schema": {
                "type": "object", "properties": {"query": {"type": "string"}},
                "required": ["query"], "additionalProperties": False,
            },
        })
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)

    assert result.output == "ROOT-EVIDENCE-136"
    assert any("LEAF-ROLE-136" in str(request["messages"]) for request in requests)
