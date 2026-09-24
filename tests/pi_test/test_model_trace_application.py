"""Model request evidence from the real Pi Application bridge."""

import json

from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.observability import inspect_run

from tests.pi_test.test_application import change_model, model_service, project


def test_pi_model_turn_records_projected_request_and_response(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)

    assert result.output == "Pi answer"
    with inspect_run(result.run) as trace:
        events = trace.events()
        started = [event for event in events if event["kind"] == "model_request" and event["runtime"] == "pi"]
        completed = [event for event in events if event["kind"] == "model_response" and event["runtime"] == "pi"]
        assert len(started) == len(completed) == len(requests) == 1
        saved = json.loads(trace.read_text(started[0]["request_ref"]))
        assert saved["model"] == requests[0][1]["model"]
        assert saved["messages"] == requests[0][1]["messages"]
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
        started = [event for event in trace.events() if event["kind"] == "model_request"]
        completed = [event for event in trace.events() if event["kind"] == "model_response"]
    assert len(started) == len(completed) == 2
    assert [event["attempt"] for event in started] == [0, 1]
    assert [event["attempt"] for event in completed] == [0, 1]
    assert {event["run_step_number"] for event in started + completed} == {1}
    assert {event["model_turn_id"] for event in started} == {
        event["model_turn_id"] for event in completed
    }
