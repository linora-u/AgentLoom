"""One native Subagent contract exercised through both supported runtimes."""

from __future__ import annotations

import json

import pytest
import yaml
from agentloom.app.run import RunLifecycleEvent
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config

from tests.application_test.mixed_runtime_support import (
    ModelReply,
    finish,
    model_service,
    project,
    runtime_events,
    tool_messages,
    write_yaml,
)


def _structured_finish(request: dict, value: object):
    if any(
        tool["function"]["name"] == "final_answer"
        for tool in request.get("tools", [])
    ):
        return finish(request, value)  # type: ignore[arg-type]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _worker_definition(
    *,
    name: str,
    runtime: str,
    workflow: str,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
) -> dict:
    definition: dict[str, object] = {
        "name": name,
        "agent_runtime": runtime,
        "model_type": "worker",
        "description": f"Execute the {name} contract.",
        "workflow": workflow,
        "tools": [],
        "toolsets": [],
    }
    if input_schema is not None:
        definition["input_schema"] = input_schema
    if output_schema is not None:
        definition["output_schema"] = output_schema
    return definition


@pytest.mark.parametrize("runtime", ["smolagents", "pi"])
def test_native_subagent_contract_has_the_same_public_result_and_lifecycle(
    tmp_path,
    runtime,
):
    expected = {
        "text": "SIMPLE-TEXT-517",
        "object": {
            "accepted": True,
            "count": 2,
            "labels": ["alpha", "beta"],
        },
        "array": ["ARRAY-ONE", "ARRAY-TWO"],
    }

    def program(request):
        messages = tool_messages(request)
        if request["model"] == "worker":
            user_messages = [
                message["content"]
                for message in request["messages"]
                if message["role"] == "user"
            ]
            supplied = "\n".join(str(item) for item in user_messages)
            if "simple request" in supplied:
                return finish(request, expected["text"])
            if '"count":2' in supplied:
                assert '"enabled":true' in supplied
                assert '"labels":["alpha","beta"]' in supplied
                return _structured_finish(request, expected["object"])
            if "array request" in supplied:
                return _structured_finish(request, expected["array"])
            raise AssertionError(f"Unexpected Worker input: {supplied}")

        if not messages:
            return [
                ("simple-call", "simple_text", {"task": "simple request"}),
                (
                    "object-call",
                    "typed_object",
                    {
                        "count": 2,
                        "enabled": True,
                        "labels": ["alpha", "beta"],
                    },
                ),
                ("array-call", "structured_array", {"task": "array request"}),
            ]
        joined = "\n".join(message["content"] for message in messages)
        assert "SIMPLE-TEXT-517" in joined
        assert "ARRAY-ONE" in joined and "ARRAY-TWO" in joined
        assert "accepted" in joined and "alpha" in joined and "beta" in joined
        return _structured_finish(request, expected)

    events = []
    with model_service(program) as (url, requests):
        workflow = project(
            tmp_path,
            url,
            supervisor=runtime,
            worker=runtime,
        )
        root = yaml.safe_load(workflow.read_text())
        root["workflow"] = "Delegate each contract exactly once, then return their values."
        root["worker_agents"] = [
            {"path": "simple.yaml"},
            {"path": "object.yaml"},
            {"path": "array.yaml"},
        ]
        root["output_schema"] = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "object": {
                    "type": "object",
                    "properties": {
                        "accepted": {"type": "boolean"},
                        "count": {"type": "integer"},
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["accepted", "count", "labels"],
                    "additionalProperties": False,
                },
                "array": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["text", "object", "array"],
            "additionalProperties": False,
        }
        write_yaml(workflow, root)

        workers = workflow.parent / "worker_agents"
        write_yaml(
            workers / "simple.yaml",
            _worker_definition(
                name="simple_text",
                runtime=runtime,
                workflow="Return the supplied task as a plain text token.",
            ),
        )
        write_yaml(
            workers / "object.yaml",
            _worker_definition(
                name="typed_object",
                runtime=runtime,
                workflow="Return a structured object preserving all typed inputs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "enabled": {"type": "boolean"},
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["count", "enabled", "labels"],
                    "additionalProperties": False,
                },
                output_schema=root["output_schema"]["properties"]["object"],
            ),
        )
        write_yaml(
            workers / "array.yaml",
            _worker_definition(
                name="structured_array",
                runtime=runtime,
                workflow="Return the requested result as a structured array.",
                output_schema={
                    "type": "array",
                    "items": {"type": "string"},
                },
            ),
        )

        with bind_config(load_project_config(tmp_path)):
            result = execute_app(
                workflow,
                file_logging=False,
                event_sink=events.append,
            )

    assert result.output == expected
    completed = [
        event
        for event in events
        if isinstance(event, RunLifecycleEvent) and event.event == "run.completed"
    ]
    assert len(completed) == 1
    assert completed[0].output == expected

    supervisor_request = next(
        request for request in requests if request["model"] == "supervisor"
    )
    tools = {
        tool["function"]["name"]: tool["function"]["parameters"]
        for tool in supervisor_request.get("tools", [])
    }
    assert tools["simple_text"]["properties"] == {
        "task": {
            "type": "string",
            "description": "Task for this Agent.",
        }
    }
    assert tools["typed_object"]["properties"]["count"]["type"] == "integer"
    assert tools["typed_object"]["properties"]["enabled"]["type"] == "boolean"
    assert tools["typed_object"]["properties"]["labels"]["type"] == "array"
    assert not any(
        tag in json.dumps(request, ensure_ascii=False)
        for request in requests
        for tag in ("<task_spec>", "<workflow>", "<task_request>", "<inputs>")
    )


@pytest.mark.parametrize(
    ("supervisor_runtime", "worker_runtime"),
    [("smolagents", "pi"), ("pi", "smolagents")],
)
def test_invalid_subagent_output_becomes_an_output_validation_tool_record(
    tmp_path,
    supervisor_runtime,
    worker_runtime,
):
    def program(request):
        messages = tool_messages(request)
        if request["model"] == "worker":
            if worker_runtime == "pi":
                assert not messages
                return ModelReply('{"findings":[3]}', "length")
            return _structured_finish(request, {"findings": [3]})

        if not messages:
            return [("invalid-call", "invalid_structured", {"task": "inspect"})]
        assert "invalid structured output" in messages[-1]["content"].lower()
        return finish(request, "Handled invalid Worker output")

    with model_service(program) as (url, _requests):
        workflow = project(
            tmp_path,
            url,
            supervisor=supervisor_runtime,
            worker=worker_runtime,
        )
        root = yaml.safe_load(workflow.read_text())
        root["workflow"] = "Call the Worker once and handle its contract failure."
        root["worker_agents"] = [{"path": "invalid.yaml"}]
        write_yaml(workflow, root)

        worker = _worker_definition(
            name="invalid_structured",
            runtime=worker_runtime,
            workflow="Return findings that satisfy the output contract.",
            output_schema={
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["findings"],
                "additionalProperties": False,
            },
        )
        worker["runtime_options"] = (
            {"max_stop_attempts": 1}
            if worker_runtime == "pi"
            else {
                "max_steps": 1,
                "smart_summary": False,
                "todo_mode": "off",
            }
        )
        write_yaml(workflow.parent / "worker_agents" / "invalid.yaml", worker)

        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)

    assert result.output == "Handled invalid Worker output"
    records = [
        event["details"]["record"]
        for event in runtime_events(result)
        if event["kind"] == "tool"
        and event["details"].get("record", {}).get("tool_name")
        == "invalid_structured"
    ]
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error"]["kind"] == "output_validation"
    assert records[0]["error"]["stage"] == "output_validation"
