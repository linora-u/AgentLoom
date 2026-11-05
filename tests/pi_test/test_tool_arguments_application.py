"""Raw tool arguments cross the published Responses SDK before tool execution."""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml
from agentloom.app.application import Application
from agentloom.execution.concurrency.batch_models import WorkItem
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.observability import inspect_run
from agentloom.execution.tool_gateway import ToolBinding
from jsonschema import Draft202012Validator

from tests.pi_test.test_application import change_model, project


def create_parameter_save_tool(context):
    artifact = Path(context.input["artifact"])
    schema = {
        "type": "object",
        "properties": {"payload": {"type": "object"}},
        "required": ["payload"],
        "additionalProperties": False,
    }

    def save(payload):
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        return {"artifact": str(artifact)}

    return ToolBinding(
        definition=ToolDefinition(
            name="save_parameters", description="Save the parameter fixture.",
            parameters=schema,
        ),
        forward=save,
        inputs_schema=schema["properties"],
        input_validator=Draft202012Validator(schema).validate,
        terminal_on_success=True,
    )


@contextmanager
def raw_arguments_service(raw_arguments: str, boundary: str = "all"):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            item = {
                "type": "function_call", "id": "fc_parameters",
                "call_id": "call_parameters", "status": "completed",
                "name": "save_parameters", "arguments": raw_arguments,
            }
            ordinary = '{"payload":{"caption":"ordinary wire value"}}'
            done_arguments = raw_arguments if boundary in {"all", "output_item_done"} else ordinary
            terminal_arguments = raw_arguments if boundary in {"all", "response_completed"} else ordinary
            events = [
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_parameters", "model": request["model"],
                        "status": "in_progress", "output": [],
                    },
                },
                {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {**item, "arguments": ""},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"], "output_index": 0,
                    "delta": raw_arguments if boundary == "all" else ordinary,
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"], "output_index": 0,
                    "arguments": raw_arguments if boundary in {"all", "arguments_done"} else ordinary,
                },
                {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {**item, "arguments": done_arguments},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_parameters", "model": request["model"],
                        "status": "completed", "output": [{**item, "arguments": terminal_arguments}],
                        "usage": {
                            "input_tokens": 12, "output_tokens": 3,
                            "total_tokens": 15,
                        },
                    },
                },
            ]
            body = b"".join(
                ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                for event in events
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_parameter_case(root: Path, raw_arguments: str, boundary: str = "all"):
    artifact = root / "parameters.json"
    with raw_arguments_service(raw_arguments, boundary) as (url, requests):
        worker_path = project(root, url)
        change_model(root, adapter="openai_responses")
        definition = yaml.safe_load(worker_path.read_text())
        definition.update(
            input_schema={
                "type": "object",
                "properties": {"artifact": {"type": "string"}},
                "required": ["artifact"], "additionalProperties": False,
            },
            tools=[{
                "name": "save_parameters",
                "factory": {"module": __name__, "function": "create_parameter_save_tool"},
            }],
        )
        worker_path.write_text(yaml.safe_dump(definition))
        with Application.open(
            worker_path.parents[1], project_root=root, file_logging=False,
        ) as app:
            outcome = app.worker("workflows/root.yaml").invoke(
                WorkItem("parameters", {"artifact": str(artifact)}),
            )
            with inspect_run(app.run) as trace:
                responses = [
                    json.loads(trace.read_text(event["response_ref"]))
                    for event in trace.events()
                    if event["kind"] == "model_response" and event.get("response_ref")
                ]
                tools = [event for event in trace.events() if event["kind"] == "tool"]
    return outcome, artifact, requests, responses, tools


@pytest.mark.parametrize(
    ("raw_arguments", "path"),
    [
        ('{"payload":{"caption":"first"},"payload":{"caption":"last"}}', "$.payload"),
        ('{"payload":{"P1":{"prosody":"first","prosody":"last"}}}', "$.payload.P1.prosody"),
    ],
    ids=["duplicate-top-level", "duplicate-nested"],
)
@pytest.mark.parametrize("boundary", ["arguments_done", "output_item_done", "response_completed"])
def test_raw_responses_duplicate_keys_cannot_save(tmp_path, raw_arguments, path, boundary):
    outcome, artifact, requests, responses, tools = run_parameter_case(tmp_path, raw_arguments, boundary)

    assert outcome.status == "failed"
    assert not artifact.exists()
    assert len(requests) == 1
    assert tools == []
    assert len(responses) == 1
    assert responses[0]["stopReason"] == "error"
    assert responses[0]["errorMessage"] == "Duplicate JSON key at " + path
    assert responses[0]["usage"]["totalTokens"] == 15
    assert "first" not in responses[0]["errorMessage"]
    assert "last" not in responses[0]["errorMessage"]


@pytest.mark.parametrize(
    ("raw_arguments", "expected"),
    [
        (
            '{"payload":{"caption":"ordinary JSON"}}',
            {"caption": "ordinary JSON"},
        ),
        (
            '{"payload":{"caption":"same text","change":"same text"}}',
            {"caption": "same text", "change": "same text"},
        ),
        (
            '{"payload":{"rows":[{"caption":"same"},{"caption":"same"}]}}',
            {"rows": [{"caption": "same"}, {"caption": "same"}]},
        ),
        (
            '{"payload":{"caption":"literal \\"key\\":\\"value\\" inside a string"}}',
            {"caption": 'literal "key":"value" inside a string'},
        ),
    ],
    ids=["ordinary", "same-values", "separate-objects", "escaped-string"],
)
def test_raw_responses_valid_json_can_save(tmp_path, raw_arguments, expected):
    outcome, artifact, requests, responses, tools = run_parameter_case(tmp_path, raw_arguments)

    assert outcome.status == "success", outcome.error
    assert outcome.output == {"artifact": str(artifact)}
    assert json.loads(artifact.read_text()) == expected
    assert len(requests) == 1
    assert len(tools) == 1
    assert tools[0]["status"] == "completed"
    assert len(responses) == 1
    assert responses[0]["stopReason"] == "toolUse"
