"""Real Application and published Pi SDK; only the model HTTP service is a fixture."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

import pytest
import yaml
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config


@contextmanager
def model_service(*, responses=False, fail_count=0, error_status=500, stall=None, finish="stop", finishes=None, stall_stream=False, turns=None, fail_requests=None, fail_messages=None, on_request=None, outputs=None):
    requests = []
    request_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with request_lock:
                requests.append((self.path, request, dict(self.headers)))
                request_number = len(requests)
            if on_request is not None:
                on_request(request_number, request)
            if stall is not None and not stall_stream:
                stall.wait(timeout=10)
            if request_number <= fail_count or request_number in (fail_requests or {}):
                self.send_response((fail_requests or {}).get(request_number, error_status))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                message = (fail_messages or {}).get(
                    request_number,
                    "PRIVATE-PROVIDER-ECHO fixture-secret",
                )
                self.wfile.write(json.dumps({
                    "error": {"message": message, "type": "server_error"},
                }).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if stall_stream:
                self.wfile.flush()
                stall.wait(timeout=5)
            output_text = (
                outputs[request_number - 1]
                if outputs is not None and request_number <= len(outputs)
                else "Pi answer"
            )
            finish_reason = (
                finishes[request_number - 1]
                if finishes is not None and request_number <= len(finishes)
                else finish
            )
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": output_text}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
            ]
            if turns is not None and request_number <= len(turns) and turns[request_number - 1] is not None:
                calls = turns[request_number - 1]
                if callable(calls):
                    calls = calls(request)
                chunks[0]["choices"][0]["delta"] = {"role": "assistant", "tool_calls": [
                    {"index": index, "id": call_id, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments)}}
                    for index, (call_id, name, arguments) in enumerate(calls)
                ]}
                chunks[1]["choices"][0]["finish_reason"] = "tool_calls"
            if finish == "tool_calls":
                if request_number == 1:
                    chunks[0]["choices"][0]["delta"]["tool_calls"] = [{"index": 0, "id": "unavailable_call", "type": "function",
                        "function": {"name": "write", "arguments": '{"path":"should-not-exist","content":"bad"}'}}]
                else:
                    chunks[1]["choices"][0]["finish_reason"] = "stop"
            if responses:
                message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": output_text, "annotations": []}]}
                events = [
                    {"type": "response.created", "response": {"id": "resp_1", "model": request["model"], "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**message, "content": []}},
                    {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0,
                     "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": output_text},
                    {"type": "response.output_item.done", "output_index": 0, "item": message},
                    {"type": "response.completed", "response": {"id": "resp_1", "model": request["model"], "status": "completed", "output": [message],
                        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15, "input_tokens_details": {"cached_tokens": 0}}}},
                ]
                for event in events:
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                return
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def project(root: Path, url: str):
    import yaml
    (root / "config").mkdir(parents=True)
    (root / "config/system.yaml").write_text("checkpoint: {enabled: false}\nself_learning: {enabled: false}\ndefault_toolsets: [core_file, core_shell, core_search]\n")
    model = {"model": "openai/fixture-model", "adapter": "openai_chat", "base_url": url,
             "api_key": "fixture-secret", "temperature": 0.25, "context_window": 8192,
             "max_output_tokens": 100, "timeout": 10, "num_retries": 0, "requests_per_minute": 2000000,
             "extra_headers": {"X-Fixture": "selected-profile"}}
    (root / "config/llm.yaml").write_text(yaml.safe_dump({"model": {"default_model_type": "test", "test": model, "summary": model}}))
    app = root / "applications/pi/workflows/root.yaml"
    app.parent.mkdir(parents=True)
    app.write_text("name: pi\nagent_runtime: pi\ndescription: Answer directly.\nworkflow: Say Pi answer.\ntools: []\ntoolsets: []\n")
    return app


def test_real_yaml_pi_no_tools_returns_receipt_and_exact_model_request(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    assert len(requests) == 1
    path, payload, headers = requests[0]
    assert path == "/v1/chat/completions"
    assert payload["model"] == "fixture-model"
    assert payload["temperature"] == 0.25
    assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 100
    assert not payload.get("tools")
    messages = payload["messages"]
    assert any(
        message["role"] == "system" and "Say Pi answer." in message["content"]
        for message in messages
    )
    assert not any(message["role"] == "user" for message in messages)
    assert headers["X-Fixture"] == "selected-profile"
    assert "todo_write" not in json.dumps(payload)
    assert "final_answer" not in json.dumps(payload)
    assert result.run.manifest_path.is_file()
    assert result.goal is None
    events = [json.loads(line) for line in (result.run.run_dir / "audit/runtime_events.jsonl").read_text().splitlines()]
    assert [e["details"]["state"] for e in events if e["kind"] == "terminal"] == ["success"]
    assert [e["details"]["total_tokens"] for e in events if e["kind"] == "usage"] == [15]


def change_model(root, **changes):
    path = root / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["test"].update(changes)
    path.write_text(yaml.safe_dump(config))


def test_responses_profile_uses_native_responses_request(tmp_path):
    with model_service(responses=True) as (url, requests):
        app = project(tmp_path, url)
        change_model(tmp_path, adapter="openai_responses", extra_body={"metadata": {"case": "responses"}})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    path, payload, _ = requests[0]
    assert path == "/v1/responses"
    assert payload["max_output_tokens"] == 100
    assert payload["metadata"] == {"case": "responses"}
    assert not payload.get("tools")


@pytest.mark.parametrize(
    ("responses", "expected_path"),
    [
        (False, "/v1/chat/completions"),
        (True, "/v1/responses"),
    ],
)
def test_structured_output_uses_native_wire_and_validates_json(
    tmp_path,
    responses,
    expected_path,
):
    with model_service(
        responses=responses,
        outputs=['{"findings":["missing guard"]}'],
    ) as (url, requests):
        app = project(tmp_path, url)
        config = yaml.safe_load(app.read_text())
        config["output_schema"] = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        app.write_text(yaml.safe_dump(config))
        if responses:
            change_model(tmp_path, adapter="openai_responses")
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)

    assert result.run.manifest_path.is_file()
    path, payload, _ = requests[0]
    assert path == expected_path
    if responses:
        assert payload["text"]["format"] == {
            "type": "json_schema",
            "name": "pi_output",
            "schema": config["output_schema"],
            "strict": True,
        }
    else:
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "pi_output",
                "schema": config["output_schema"],
                "strict": True,
            },
        }


def test_invalid_structured_output_is_corrected_in_same_session_without_tools(
    tmp_path,
):
    with model_service(
        outputs=["not-json", '{"findings":["fixed"]}'],
    ) as (url, requests):
        app = project(tmp_path, url)
        config = yaml.safe_load(app.read_text())
        config["output_schema"] = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        app.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)

    assert result.run.manifest_path.is_file()
    assert len(requests) == 2
    assert not requests[1][1].get("tools")
    assert "not valid JSON" in json.dumps(requests[1][1])


def test_provider_failure_during_structured_correction_stays_provider_error(
    tmp_path,
):
    from agentloom.app.run import ApplicationRunError

    with model_service(
        outputs=["not-json"],
        fail_requests={2: 500},
    ) as (url, requests):
        app = project(tmp_path, url)
        config = yaml.safe_load(app.read_text())
        config["output_schema"] = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        app.write_text(yaml.safe_dump(config))

        with (
            bind_config(load_project_config(tmp_path)),
            pytest.raises(ApplicationRunError) as captured,
        ):
            execute_app(app, file_logging=False)

    assert captured.value.original_error.category == "provider"
    assert len(requests) == 2


def test_invalid_structured_output_at_budget_exhaustion_is_output_validation(
    tmp_path,
):
    from agentloom.app.run import ApplicationRunError

    with model_service(outputs=["not-json"], finish="length") as (url, requests):
        app = project(tmp_path, url)
        config = yaml.safe_load(app.read_text())
        config["output_schema"] = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        app.write_text(yaml.safe_dump(config))

        with (
            bind_config(load_project_config(tmp_path)),
            pytest.raises(ApplicationRunError) as captured,
        ):
            execute_app(app, file_logging=False)

    assert captured.value.original_error.category == "output_validation"
    assert captured.value.original_error.kind == "output_validation"
    assert captured.value.original_error.stage == "output_validation"
    assert len(requests) == 1


def test_invalid_structured_output_consumes_the_existing_delivery_budget(
    tmp_path,
):
    from agentloom.app.run import ApplicationRunError

    with model_service(
        outputs=["not-json", "still-not-json", "not-json-again"],
        finishes=["stop", "stop", "length"],
    ) as (url, requests):
        app = project(tmp_path, url)
        config = yaml.safe_load(app.read_text())
        config["runtime_options"] = {"max_stop_attempts": 2}
        config["output_schema"] = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        app.write_text(yaml.safe_dump(config))

        with (
            bind_config(load_project_config(tmp_path)),
            pytest.raises(ApplicationRunError) as captured,
        ):
            execute_app(app, file_logging=False)

    assert captured.value.original_error.category == "output_validation"
    assert len(requests) == 2


def test_structured_correction_and_stop_gate_share_one_delivery_budget(
    tmp_path,
):
    from agentloom.app.run import ApplicationRunError

    valid_output = json.dumps({"findings": []})
    with model_service(outputs=["not-json", valid_output, valid_output]) as (
        url,
        requests,
    ):
        app = project(tmp_path, url)
        stop_hook = tmp_path / "reject_stop.py"
        stop_hook.write_text(
            "import json\n"
            "print(json.dumps({'decision': 'block', 'reason': 'Verify again.'}))\n"
        )
        import sys

        config = yaml.safe_load(app.read_text())
        config["runtime_options"] = {"max_stop_attempts": 2}
        config["output_schema"] = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        config["hooks"] = {
            "Stop": [
                {
                    "id": "gate",
                    "command": f"{sys.executable} {stop_hook}",
                }
            ]
        }
        app.write_text(yaml.safe_dump(config))

        with (
            bind_config(load_project_config(tmp_path)),
            pytest.raises(ApplicationRunError, match="Stop gate remained blocked"),
        ):
            execute_app(app, file_logging=False)

    assert len(requests) == 2


@pytest.mark.parametrize("status,retries,expected", [(500, 2, 2), (429, 1, 2), (401, 3, 1), (400, 3, 1), (500, 0, 1)])
def test_profile_retry_count_and_private_error_redaction(tmp_path, status, retries, expected):
    from agentloom.app.run import ApplicationRunError
    with model_service(fail_count=1, error_status=status) as (url, requests):
        app = project(tmp_path, url)
        change_model(tmp_path, num_retries=retries, retry_delay=0.01, max_retry_delay=0.01)
        with bind_config(load_project_config(tmp_path)):
            if expected == 2:
                assert execute_app(app, file_logging=False).output == "Pi answer"
            else:
                with pytest.raises(ApplicationRunError) as error:
                    execute_app(app, file_logging=False)
                assert error.value.original_error.category == "provider"
                assert "PRIVATE-PROVIDER-ECHO" not in str(error.value)
                assert "fixture-secret" not in str(error.value)
                assert error.value.run.manifest_path.is_file()
    assert len(requests) == expected


@pytest.mark.parametrize("selection,match", [
    ("runtime_options: {thinking: low}\n", "Unsupported pi runtime_options"),
])
def test_unsupported_features_rejected_at_public_application_boundary(tmp_path, selection, match):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        app.write_text(app.read_text() + selection)
        with bind_config(load_project_config(tmp_path)), pytest.raises(ValueError, match=match):
            execute_app(app, file_logging=False)
    assert not requests


@pytest.mark.parametrize("changes,match", [
    ({"adapter": "anthropic_messages"}, "protocol"),
    ({"unsupported_parameter": 1}, "unsupported parameters"),
    ({"extra_body": {"tools": [{}]}}, "extra_body"),
    ({"system_prompt_boundary": "something"}, "system_prompt_boundary"),
])
def test_unsupported_model_settings_fail_without_http_call(tmp_path, changes, match):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        change_model(tmp_path, **changes)
        with bind_config(load_project_config(tmp_path)), pytest.raises((ValueError, RuntimeError), match=match):
            execute_app(app, file_logging=False)
    assert not requests


@pytest.mark.parametrize("allow_second", [True, False])
def test_stop_hook_can_continue_same_native_session_or_reject(tmp_path, allow_second):
    from agentloom.app.run import ApplicationRunError
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        marker = tmp_path / "stop-count"
        script = tmp_path / "stop.py"
        script.write_text("from pathlib import Path\nimport json\np=Path(" + repr(str(marker)) + ")\n"
            "n=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n"
            f"print(json.dumps({{'decision': 'allow' if n > 0 and {allow_second!r} else 'block', 'reason': 'Please verify the answer.'}}))\n")
        import sys
        config = yaml.safe_load(app.read_text())
        config.update(runtime_options={"max_stop_attempts": 2}, hooks={"Stop": [{"id": "gate", "command": f"{sys.executable} {script}"}]})
        app.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)):
            if allow_second:
                assert execute_app(app, file_logging=False).output == "Pi answer"
            else:
                with pytest.raises(ApplicationRunError, match="Stop gate remained blocked"):
                    execute_app(app, file_logging=False)
    assert marker.read_text() == "2"
    assert len(requests) == 2
    assert "Please verify" in json.dumps(requests[1][1])
    assert any(m["role"] == "assistant" for m in requests[1][1]["messages"])


def test_project_pi_extensions_skills_and_context_files_are_not_discovered(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        (tmp_path / "AGENTS.md").write_text("UNSELECTED_CONTEXT_SHOULD_NOT_APPEAR")
        ext = tmp_path / ".pi/extensions/unselected.ts"
        ext.parent.mkdir(parents=True)
        ext.write_text("throw new Error('UNSELECTED_EXTENSION_LOADED');")
        (tmp_path / ".pi/settings.json").write_text('{"defaultProvider":"invalid","defaultModel":"invalid"}')
        with bind_config(load_project_config(tmp_path)):
            assert execute_app(app, file_logging=False).output == "Pi answer"
    assert "UNSELECTED_CONTEXT" not in json.dumps(requests[0][1])


@pytest.mark.parametrize("finish,match", [("length", "max_steps_error"), ("tool_calls", "unavailable tool")])
def test_incomplete_or_unselected_tool_turn_does_not_report_success(tmp_path, finish, match):
    from agentloom.app.run import ApplicationRunError
    with model_service(finish=finish) as (url, requests):
        app = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError, match=match):
            execute_app(app, file_logging=False)
    assert len(requests) == 1
    assert not (tmp_path / "should-not-exist").exists()


def test_profile_timeout_bounds_an_open_sse_stream(tmp_path):
    from threading import Event

    from agentloom.app.run import ApplicationRunError
    release = Event()
    request_times = []
    with model_service(
        stall=release,
        stall_stream=True,
        on_request=lambda *_: request_times.append(time.monotonic()),
    ) as (url, requests):
        app = project(tmp_path, url)
        change_model(tmp_path, timeout=1)
        try:
            with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError, match="timed out"):
                execute_app(app, file_logging=False)
            failed_at = time.monotonic()
        finally:
            release.set()
    assert len(requests) == len(request_times) == 1
    assert failed_at - request_times[0] < 4
