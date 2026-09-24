"""Model request evidence from the real Pi Application bridge."""

import json
import subprocess

import pytest
import yaml
from agentloom.app.run import ApplicationRunError
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.observability import inspect_run
from agentloom.execution.subprocess_env import build_subprocess_env
from agentloom.runtimes.pi.capture import read_model_capture
from agentloom.runtimes.pi.install import find_node, installed_pi_entry

from tests.pi_test.test_application import change_model, model_service, project


def test_pi_model_capture_rejects_oversize_file(tmp_path):
    capture_id = 'a' * 32
    with (tmp_path / f'model-{capture_id}.json').open('wb') as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='32 MiB'):
        read_model_capture(tmp_path, capture_id, '0' * 64)


def test_pi_model_turn_records_projected_request_and_response(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)

    assert result.output == "Pi answer"
    with inspect_run(result.run) as trace:
        events = trace.events()
        started = [event for event in events if event["kind"] == "model_request"
                   and event["runtime"] == "pi" and event["boundary"] == "pi_payload"]
        wire = [event for event in events if event["kind"] == "model_request"
                and event["boundary"] == "openai_http_request"]
        completed = [event for event in events if event["kind"] == "model_response" and event["runtime"] == "pi"]
        assert len(started) == len(wire) == len(completed) == len(requests) == 1
        saved = json.loads(trace.read_text(started[0]["request_ref"]))
        assert saved["model"] == requests[0][1]["model"]
        assert saved["messages"] == requests[0][1]["messages"]
        assert json.loads(trace.read_text(wire[0]["request_ref"]))["body"] == requests[0][1]
        assert "Pi answer" in trace.read_text(completed[0]["response_ref"])
        assert started[0]["model_turn_id"] == completed[0]["model_turn_id"]
        assert started[0]["step_number"] == 1


def test_pi_retry_attempts_share_one_step_and_are_distinguishable(tmp_path):
    with model_service(fail_requests={1: 500}) as (url, requests):
        app = project(tmp_path, url)
        change_model(tmp_path, num_retries=1, retry_delay=0.01)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)

    assert result.output == "Pi answer"
    assert len(requests) == 2
    with inspect_run(result.run) as trace:
        started = [event for event in trace.events() if event["kind"] == "model_request"
                   and event["boundary"] == "pi_payload"]
        wire = [event for event in trace.events() if event["kind"] == "model_request"
                and event["boundary"] == "openai_http_request"]
        completed = [event for event in trace.events() if event["kind"] == "model_response"]
    assert len(started) == len(wire) == len(completed) == 2
    assert [event["attempt"] for event in started] == [0, 1]
    assert [event["attempt"] for event in wire] == [0, 1]
    assert [event["attempt"] for event in completed] == [0, 1]
    assert {event["run_step_number"] for event in started + completed} == {1}
    assert {event["model_turn_id"] for event in started} == {
        event["model_turn_id"] for event in completed
    }
    assert [event["status"] for event in completed] == ["error", "completed"]


def test_pi_failed_model_response_is_recorded_as_error(tmp_path):
    with model_service(fail_count=1, error_status=400) as (url, requests):
        app = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
            execute_app(app, file_logging=False)

    assert len(requests) == 1
    with inspect_run(failure.value.run) as trace:
        responses = [event for event in trace.events() if event["kind"] == "model_response"]
    assert len(responses) == 1
    assert responses[0]["status"] == "error"
    assert responses[0]["error"]


def test_pi_abort_before_next_provider_request_has_no_unpaired_response(tmp_path):
    unavailable = [("unknown_call", "write", {"path": "should-not-exist", "content": "bad"})]
    with model_service(turns=[unavailable]) as (url, requests):
        app = project(tmp_path, url)
        config = yaml.safe_load(app.read_text())
        config["runtime_options"] = {"max_stop_attempts": 1}
        app.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
            execute_app(app, file_logging=False)

    assert len(requests) == 1
    with inspect_run(failure.value.run) as trace:
        events = trace.events()
    requests_in_trace = [event for event in events if event["kind"] == "model_request"
                         and event["boundary"] == "pi_payload"]
    responses = [event for event in events if event["kind"] == "model_response"]
    assert len(requests_in_trace) == len(responses) == 1
    assert requests_in_trace[0]["model_turn_id"] == responses[0]["model_turn_id"]


def test_pi_bridge_pairs_post_request_transport_exception(tmp_path):
    script = """
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const {configureModel} = await import(pathToFileURL(process.argv[1]).href);
const directory = process.argv[2];
const calls = [];
const model = {api: 'openai-completions', provider: 'agentloom', id: 'fixture-model'};
const session = {agent: {streamFn: async (_model, _context, options) => {
  await options.onPayload({model: 'fixture-model', messages: [], tools: []}, model);
  throw new Error('fixture post-request transport exception');
}}};
configureModel(session,
  {timeout: 5, requests_per_minute: 2000000, num_retries: 0, retry_delay: 0.01, max_retry_delay: 0.01},
  {}, async () => ({state: 'work', agent_context: [], identity: {call_id: 'model:fixture'}}),
  () => {}, directory,
  async payload => {
    calls.push(payload);
    return {method: 'model_trace', phase: payload.phase, attempt: payload.attempt, accepted: true};
  });
const stream = await session.agent.streamFn(model, {messages: [], tools: []}, {});
const result = await stream.result();
const captured = JSON.parse(await readFile(join(directory, `model-${calls.at(-1).capture_id}.json`), 'utf8'));
console.log(JSON.stringify({phases: calls.map(call => call.phase),
  stopReason: result.stopReason, error: captured.errorMessage}));
"""
    entry = installed_pi_entry().with_name("model.js")
    completed = subprocess.run(
        [find_node(build_subprocess_env()), "--input-type=module", "-e", script, str(entry), str(tmp_path)],
        capture_output=True, text=True, check=True, timeout=15,
    )
    observed = json.loads(completed.stdout)
    assert observed == {
        "phases": ["request", "response"], "stopReason": "error",
        "error": "fixture post-request transport exception",
    }
