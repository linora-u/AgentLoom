"""Real Application and published Pi SDK; only the model HTTP service is a fixture."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
import time

import pytest
import yaml

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config


@contextmanager
def model_service(*, responses=False, fail_count=0, error_status=500, stall=None, finish="stop"):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, request, dict(self.headers)))
            if stall is not None:
                stall.wait(timeout=10)
            if len(requests) <= fail_count:
                self.send_response(error_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"PRIVATE-PROVIDER-ECHO fixture-secret","type":"server_error"}}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Pi answer"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
            ]
            if responses:
                message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "Pi answer", "annotations": []}]}
                events = [
                    {"type": "response.created", "response": {"id": "resp_1", "model": request["model"], "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**message, "content": []}},
                    {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0,
                     "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": "Pi answer"},
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
    (root / "config/system.yaml").write_text("lsp_servers: {enabled: false}\ncheckpoint: {enabled: false}\nself_learning: {enabled: false}\ndefault_toolsets: [core_file, core_shell, core_search]\n")
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


@pytest.mark.parametrize("status,retries,expected", [(500, 2, 2), (429, 1, 2), (401, 3, 1), (400, 3, 1), (500, 0, 1)])
def test_profile_retry_count_and_private_error_redaction(tmp_path, status, retries, expected):
    from agentloom.application.run import ApplicationRunError
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
    ("goal: {enabled: true}\n", "goal"),
    ("checkpoint: {enabled: true}\n", "checkpoint_resume"),
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
    from agentloom.application.run import ApplicationRunError
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
