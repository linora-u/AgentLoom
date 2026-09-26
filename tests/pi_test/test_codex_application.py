"""Pi Codex subscription requests through a real Application and SDK Agent loop."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from agentloom.app.run import ApplicationRunError
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.observability import inspect_run
from agentloom.execution.subprocess_env import build_subprocess_env
from agentloom.runtimes.pi.install import find_node

from tests.pi_test.test_application import project


def _fixture(tmp_path: Path, monkeypatch, *, search: str, completed: bool = True,
             citations: bool = True, status: int = 200, refresh: bool = False,
             refresh_error: bool = False, first_textual_tool_call: bool = False,
             answer: str = "Current result", first_status: int | None = None,
             first_answer: str | None = None):
    app = project(tmp_path, "http://127.0.0.1:1/v1")
    configuration = tmp_path / "config/llm.yaml"
    data = yaml.safe_load(configuration.read_text())
    data["model"]["codex"] = {
        "adapter": "openai_codex_responses", "model": "gpt-6-luna",
        "context_window": 272000, "max_output_tokens": 256,
        "timeout": 10, "num_retries": 0, "requests_per_minute": 2000000,
        "reasoning_effort": "xhigh", "web_search": search,
    }
    configuration.write_text(yaml.safe_dump(data))
    definition = yaml.safe_load(app.read_text())
    definition["model_type"] = "codex"
    app.write_text(yaml.safe_dump(definition))

    auth_dir = tmp_path / "pi-auth"
    auth_dir.mkdir()
    account = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"}
    }).encode()).decode().rstrip("=")
    credential = auth_dir / "auth.json"
    credential.write_text(json.dumps({"openai-codex": {
        "type": "oauth", "access": f"fixture.{account}.signature",
        "refresh": "fixture-refresh", "expires": int(time.time() * 1000) + (-1000 if refresh else 3600_000),
    }}))
    credential.chmod(0o600)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(auth_dir))

    wire = tmp_path / "codex-wire.jsonl"
    scenario = tmp_path / "codex-scenario.json"
    scenario.write_text(json.dumps({"completed": completed, "citations": citations,
                                    "status": status, "refresh": refresh,
                                    "refresh_error": refresh_error,
                                    "first_textual_tool_call": first_textual_tool_call,
                                    "answer": answer, "first_status": first_status,
                                    "first_answer": first_answer}))
    bootstrap = tmp_path / "codex-bootstrap.mjs"
    bootstrap.write_text("""
import {appendFileSync, readFileSync} from 'node:fs';
import {zstdDecompressSync} from 'node:zlib';
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  if (request.url === 'https://auth.openai.com/oauth/token') {
    const scenario = JSON.parse(readFileSync(%SCENARIO%, 'utf8'));
    if (!scenario.refresh) throw new Error('unexpected OAuth refresh');
    appendFileSync(%REFRESH%, 'refreshed\\n');
    if (scenario.refresh_error) return new Response(JSON.stringify({error: 'invalid_grant'}),
      {status: 401, headers: {'content-type': 'application/json'}});
    return new Response(JSON.stringify({access_token: %ACCESS%, refresh_token: 'fixture-new-refresh',
      expires_in: 3600}), {status: 200, headers: {'content-type': 'application/json'}});
  }
  if (!request.url.endsWith('/backend-api/codex/responses')) return nativeFetch(input, init);
  let body = Buffer.from(await request.arrayBuffer());
  if (request.headers.get('content-encoding') === 'zstd') body = zstdDecompressSync(body);
  const payload = JSON.parse(body.toString('utf8'));
  appendFileSync(%WIRE%, JSON.stringify({url: request.url, body: payload}) + '\\n');
  const scenario = JSON.parse(readFileSync(%SCENARIO%, 'utf8'));
  const count = readFileSync(%WIRE%, 'utf8').trim().split('\\n').length;
  const status = count === 1 && scenario.first_status ? scenario.first_status : scenario.status;
  if (status !== 200) {
    const message = status === 429 ? 'Monthly usage limit reached' : 'model unavailable';
    return new Response(JSON.stringify({error: {message, type: 'provider_error'}}),
      {status, headers: {'content-type': 'application/json'}});
  }
  const answer = scenario.first_textual_tool_call && count === 1
    ? '<｜DSML｜tool_calls>\\n<｜DSML｜invoke name="unknown_tool">'
    : count === 1 && scenario.first_answer !== null ? scenario.first_answer : scenario.answer;
  const message = {type: 'message', id: 'msg_1', role: 'assistant', status: 'completed',
    content: [{type: 'output_text', text: answer, annotations: scenario.citations
      ? [{type: 'url_citation', url: 'https://example.org/news', title: 'Example News', start_index: 0, end_index: 7}]
      : []}]};
  const output = scenario.completed
    ? [{type: 'web_search_call', id: 'search_1', status: 'completed'}, message] : [message];
  const events = [
    {type: 'response.created', response: {id: 'resp_1', model: payload.model, status: 'in_progress', output: []}},
    ...(scenario.completed ? [{type: 'response.output_item.done', output_index: 0, item: output[0]}] : []),
    {type: 'response.output_item.added', output_index: 1, item: {...message, content: []}},
    {type: 'response.content_part.added', item_id: 'msg_1', output_index: 1, content_index: 0,
      part: {type: 'output_text', text: '', annotations: []}},
    {type: 'response.output_text.delta', item_id: 'msg_1', output_index: 1, content_index: 0, delta: answer},
    {type: 'response.output_item.done', output_index: 1, item: message},
    {type: 'response.completed', response: {id: 'resp_1', model: payload.model, status: 'completed', output,
      usage: {input_tokens: 12, output_tokens: 3, total_tokens: 15,
        input_tokens_details: {cached_tokens: 0}}}},
  ];
  return new Response(events.map(event => `event: ${event.type}\\ndata: ${JSON.stringify(event)}\\n\\n`).join(''),
    {status: 200, headers: {'content-type': 'text/event-stream'}});
};
""".replace("%WIRE%", json.dumps(str(wire))).replace("%SCENARIO%", json.dumps(str(scenario)))
       .replace("%REFRESH%", json.dumps(str(tmp_path / "refresh.log")))
       .replace("%ACCESS%", json.dumps(f"fixture.{account}.signature")))
    real_node = find_node(build_subprocess_env())
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    launcher = launcher_dir / "node"
    launcher.write_text(f"#!{sys.executable}\nimport os,sys\n"
                        f"binary={real_node!r}; bootstrap={str(bootstrap)!r}\n"
                        "args=sys.argv[1:]\n"
                        "if any(arg.endswith('/pi/bridge/dist/index.js') for arg in args):\n"
                        " args=['--import',bootstrap,*args]\n"
                        "os.execv(binary,[binary,*args])\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(launcher_dir) + os.pathsep + os.environ["PATH"])
    return app, wire


@pytest.mark.parametrize("mode", ["off", "auto", "required"])
def test_codex_search_mode_request_and_evidence(tmp_path, monkeypatch, mode):
    app, wire = _fixture(tmp_path, monkeypatch, search=mode, completed=mode != "off")
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    requests = [json.loads(line) for line in wire.read_text().splitlines()]
    assert len(requests) == 1
    payload = requests[0]["body"]
    assert requests[0]["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert payload["model"] == "gpt-6-luna"
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    if mode == "off":
        assert not any(tool.get("type") == "web_search" for tool in payload.get("tools", []))
    else:
        assert {"type": "web_search"} in payload["tools"]
    if mode == "required":
        assert payload["tool_choice"] == {"type": "web_search"}
    else:
        assert payload.get("tool_choice") != {"type": "web_search"}
    with inspect_run(result.run) as trace:
        events = trace.events()
        responses = [event for event in events if event["kind"] == "model_response"]
        assert len(responses) == 1
        captured = json.loads(trace.read_text(responses[0]["response_ref"]))
        wire_events = [event for event in events if event["kind"] == "model_request"
                       and event["boundary"] == "openai_http_request"]
        recorded_request = json.loads(trace.read_text(wire_events[0]["request_ref"]))
        assert all(name.lower() not in {"authorization", "cookie", "chatgpt-account-id"}
                   for name in recorded_request["headers"])
    if mode == "off":
        assert "native_search" not in captured
        assert result.output == "Current result"
    else:
        assert captured["native_search"]["calls"] == [{"id": "search_1", "status": "completed"}]
        assert captured["native_search"]["citations"] == [
            {"url": "https://example.org/news", "title": "Example News"}]
        assert "[Example News](<https://example.org/news>)" in result.output
        assert responses[0]["step_number"] == 1
        assert responses[0]["attempt"] == 0
        audit = [json.loads(line) for line in (result.run.run_dir / "audit/runtime_events.jsonl").read_text().splitlines()]
        search_events = [event for event in audit if event["kind"] == "model"
                         and event["details"].get("phase") == "web_search"]
        assert len(search_events) == 1
        assert search_events[0]["details"]["attempt"] == 0
        assert search_events[0]["details"]["completed_calls"] == captured["native_search"]["calls"]


def test_required_search_without_completed_call_fails(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="required", completed=False)
    with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
        execute_app(app, file_logging=False)
    assert len(wire.read_text().splitlines()) == 1
    assert "required web search was not executed" in str(failure.value)


def test_completed_search_without_structured_citation_adds_no_source(tmp_path, monkeypatch):
    app, _ = _fixture(tmp_path, monkeypatch, search="required", citations=False)
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output == "Current result"
    with inspect_run(result.run) as trace:
        responses = [event for event in trace.events() if event["kind"] == "model_response"]
        captured = json.loads(trace.read_text(responses[0]["response_ref"]))
    assert captured["native_search"]["calls"]
    assert captured["native_search"]["citations"] == []


def test_pi_refreshes_saved_codex_login_and_reuses_it_next_run(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="off", refresh=True)
    with bind_config(load_project_config(tmp_path)):
        first = execute_app(app, file_logging=False)
        second = execute_app(app, file_logging=False)
    assert first.output == second.output == "Current result"
    assert len(wire.read_text().splitlines()) == 2
    assert (tmp_path / "refresh.log").read_text().splitlines() == ["refreshed"]
    saved = json.loads((tmp_path / "pi-auth/auth.json").read_text())
    assert saved["openai-codex"]["refresh"] == "fixture-new-refresh"


def test_pi_refresh_failure_is_clear_and_sends_no_model_request(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="off", refresh=True,
                         refresh_error=True)
    with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
        execute_app(app, file_logging=False)
    assert "Pi Codex credential refresh failed" in str(failure.value)
    assert not wire.exists()


def test_missing_pi_login_fails_without_provider_request(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="off")
    (tmp_path / "pi-auth/auth.json").unlink()
    with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
        execute_app(app, file_logging=False)
    assert "Pi Codex login is missing" in str(failure.value)
    assert not wire.exists()


def test_unlisted_codex_model_fails_without_provider_request(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="off")
    path = tmp_path / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["codex"]["model"] = "gpt-6-unlisted-fixture"
    path.write_text(yaml.safe_dump(config))
    with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
        execute_app(app, file_logging=False)
    assert "Pi Codex model gpt-6-unlisted-fixture is unavailable" in str(failure.value)
    assert not wire.exists()


@pytest.mark.parametrize(("status", "message"), [
    (429, "Pi Codex subscription quota exhausted"),
    (404, "Pi Codex model is unavailable"),
])
def test_codex_provider_failures_do_not_fallback(tmp_path, monkeypatch, status, message):
    app, wire = _fixture(tmp_path, monkeypatch, search="off", status=status)
    with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
        execute_app(app, file_logging=False)
    assert message in str(failure.value)
    assert len(wire.read_text().splitlines()) == 1


def test_codex_max_reasoning_is_an_explicit_profile_setting(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="off")
    path = tmp_path / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["codex"]["reasoning_effort"] = "max"
    path.write_text(yaml.safe_dump(config))
    with bind_config(load_project_config(tmp_path)):
        execute_app(app, file_logging=False)
    assert json.loads(wire.read_text().splitlines()[0])["body"]["reasoning"]["effort"] == "max"


def test_required_search_is_enforced_on_each_model_turn(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="required",
                         first_textual_tool_call=True)
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output.startswith("Current result")
    requests = [json.loads(line)["body"] for line in wire.read_text().splitlines()]
    assert len(requests) == 2
    assert all(request["tool_choice"] == {"type": "web_search"} for request in requests)
    with inspect_run(result.run) as trace:
        responses = [event for event in trace.events() if event["kind"] == "model_response"]
        assert len(responses) == 2
        assert all(json.loads(trace.read_text(event["response_ref"]))["native_search"]["calls"]
                   for event in responses)
        assert len({event["model_turn_id"] for event in responses}) == 2


def test_required_search_stays_on_for_retry_attempt(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="required", first_status=500)
    path = tmp_path / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["codex"].update(num_retries=1, retry_delay=0.01, max_retry_delay=0.01)
    path.write_text(yaml.safe_dump(config))
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output.startswith("Current result")
    requests = [json.loads(line)["body"] for line in wire.read_text().splitlines()]
    assert len(requests) == 2
    assert all(request["tool_choice"] == {"type": "web_search"} for request in requests)
    with inspect_run(result.run) as trace:
        responses = [event for event in trace.events() if event["kind"] == "model_response"]
        assert [event["attempt"] for event in responses] == [0, 1]
        assert [event["status"] for event in responses] == ["error", "completed"]
        assert len({event["model_turn_id"] for event in responses}) == 2
        assert {event["run_step_number"] for event in responses} == {1}
        assert json.loads(trace.read_text(responses[1]["response_ref"]))["native_search"]["calls"]


def test_required_search_stays_on_for_stop_continuation(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="required")
    marker = tmp_path / "stop-count"
    script = tmp_path / "stop.py"
    script.write_text("from pathlib import Path\nimport json\np=Path(" + repr(str(marker)) + ")\n"
                      "n=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n"
                      "print(json.dumps({'decision': 'block' if n == 0 else 'allow', 'reason': 'Check again.'}))\n")
    definition = yaml.safe_load(app.read_text())
    definition["runtime_options"] = {"max_stop_attempts": 2}
    definition["hooks"] = {"Stop": [{"id": "gate", "command": f"{sys.executable} {script}"}]}
    app.write_text(yaml.safe_dump(definition))
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output.startswith("Current result")
    assert marker.read_text() == "2"
    requests = [json.loads(line)["body"] for line in wire.read_text().splitlines()]
    assert len(requests) == 2
    assert all(request["tool_choice"] == {"type": "web_search"} for request in requests)


def test_required_search_stays_on_for_output_correction(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="required",
                         first_answer="not-json", answer='{"answer":"Current result"}')
    definition = yaml.safe_load(app.read_text())
    definition["runtime_options"] = {"max_stop_attempts": 2}
    definition["output_schema"] = {
        "type": "object", "properties": {"answer": {"type": "string"}},
        "required": ["answer"], "additionalProperties": False,
    }
    app.write_text(yaml.safe_dump(definition))
    path = tmp_path / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["codex"]["supports_structured_output"] = True
    path.write_text(yaml.safe_dump(config))
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output == {"answer": "Current result"}
    requests = [json.loads(line)["body"] for line in wire.read_text().splitlines()]
    assert len(requests) == 2
    assert all(request["tool_choice"] == {"type": "web_search"} for request in requests)


def test_codex_structured_result_remains_json_with_search_evidence(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="required",
                         answer='{"answer":"Current result"}')
    definition = yaml.safe_load(app.read_text())
    definition["output_schema"] = {
        "type": "object", "properties": {"answer": {"type": "string"}},
        "required": ["answer"], "additionalProperties": False,
    }
    app.write_text(yaml.safe_dump(definition))
    path = tmp_path / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["codex"]["supports_structured_output"] = True
    path.write_text(yaml.safe_dump(config))
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output == {"answer": "Current result"}
    request = json.loads(wire.read_text().splitlines()[0])["body"]
    assert request["text"]["format"]["type"] == "json_schema"


def test_codex_preserves_optional_native_tool_fields_without_strict_schema(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="auto")
    definition = yaml.safe_load(app.read_text())
    definition["tools"] = [{"name": "read"}]
    app.write_text(yaml.safe_dump(definition))
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    assert result.output.startswith("Current result")
    request = json.loads(wire.read_text().splitlines()[0])["body"]
    read = next(tool for tool in request["tools"] if tool.get("name") == "read")
    assert read["strict"] is False
    assert read["parameters"]["required"] == ["path"]
    assert {"path", "limit", "offset"} == set(read["parameters"]["properties"])
    assert {"type": "web_search"} in request["tools"]


@pytest.mark.parametrize(("change", "message"), [
    ({"web_search": "always"}, "web_search must be off, auto or required"),
    ({"reasoning_effort": "ultra"}, "reasoning_effort must be xhigh or max"),
    ({"api_key": "not-a-subscription"}, "requires Pi OAuth"),
    ({"base_url": "https://example.org/v1"}, "requires Pi OAuth"),
    ({"extra_headers": {"Authorization": "Bearer fake"}}, "requires Pi OAuth"),
])
def test_codex_profile_rejects_invalid_settings_before_http(tmp_path, monkeypatch, change, message):
    app, wire = _fixture(tmp_path, monkeypatch, search="off")
    path = tmp_path / "config/llm.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"]["codex"].update(change)
    path.write_text(yaml.safe_dump(config))
    with bind_config(load_project_config(tmp_path)), pytest.raises((ValueError, RuntimeError), match=message):
        execute_app(app, file_logging=False)
    assert not wire.exists()


def test_codex_profile_requires_pi_runtime(tmp_path, monkeypatch):
    app, wire = _fixture(tmp_path, monkeypatch, search="off")
    definition = yaml.safe_load(app.read_text())
    definition["agent_runtime"] = "smolagents"
    app.write_text(yaml.safe_dump(definition))
    with bind_config(load_project_config(tmp_path)), pytest.raises((ValueError, RuntimeError),
                                                               match="requires agent_runtime: pi"):
        execute_app(app, file_logging=False)
    assert not wire.exists()
