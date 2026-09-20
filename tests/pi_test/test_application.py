"""Real Application and published Pi SDK; only the model HTTP service is a fixture."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config


@contextmanager
def model_service():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, request, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Pi answer"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
            ]
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
             "max_output_tokens": 100, "timeout": 10, "num_retries": 0,
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
