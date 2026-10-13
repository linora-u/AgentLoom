"""Formal Pi/smol CLI output intent observed at real loopback HTTP servers."""
import json
from contextlib import ExitStack

import pytest
import yaml

from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.observability import inspect_run
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_budget_litellm_http import json_model_service
from tests.pi_test.test_process_lifecycle import node_launcher, start_cli


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
@pytest.mark.parametrize("mode", ["omitted", "explicit", "cleared"])
def test_formal_cli_preserves_output_cap_at_final_provider_wire(tmp_path, runtime, mode):
    with ExitStack() as stack:
        url, requests = stack.enter_context(model_service() if runtime == "pi" else json_model_service(smol=True))
        application = project(tmp_path, url)
        profile_path = tmp_path / "config/model.yaml"
        profiles = yaml.safe_load(profile_path.read_text())
        profiles["model"]["test"].update(context_window=500000, max_output_tokens=32768)
        if mode == "omitted":
            profiles["model"]["test"].pop("max_output_tokens")
        profile_path.write_text(yaml.safe_dump(profiles))
        if mode == "cleared":
            local = application.parent.parent / "config"
            local.mkdir()
            (local / "model.local.yaml").write_text("model:\n  test:\n    max_output_tokens: null\n")
        if runtime == "smolagents":
            definition = yaml.safe_load(application.read_text())
            definition.update(agent_runtime=runtime, runtime_options={"smart_summary": False, "max_steps": 2})
            application.write_text(yaml.safe_dump(definition))
        env, _ = node_launcher(tmp_path)
        child = start_cli(tmp_path, application, env)
        stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, stderr
    events = [json.loads(line) for line in stdout.splitlines()]
    assert events[-1]["event"] == "run.completed"
    assert len(requests) == 1
    payload = requests[0][1]
    aliases = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
    if mode == "explicit":
        assert aliases.intersection(payload) == {"max_tokens"}
        assert payload["max_tokens"] == 32768
    else:
        assert not aliases.intersection(payload)
    (tmp_path / "provider-wire.json").write_text(json.dumps({"runtime": runtime, "mode": mode,
        "path": requests[0][0], "body": payload, "events": events, "remote_model_requests": 0}, indent=2))


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_final_http_trace_keeps_input_reserve_source_separate_from_output_cap(tmp_path, runtime):
    with ExitStack() as stack:
        url, requests = stack.enter_context(model_service() if runtime == "pi" else json_model_service(smol=True))
        application = project(tmp_path, url)
        profile_path = tmp_path / "config/model.yaml"
        profiles = yaml.safe_load(profile_path.read_text())
        profiles["model"]["test"].update(context_window=500000, max_output_tokens=None, input_token_reserve=8192)
        profile_path.write_text(yaml.safe_dump(profiles))
        if runtime == "smolagents":
            definition = yaml.safe_load(application.read_text())
            definition.update(agent_runtime=runtime, runtime_options={"smart_summary": False, "max_steps": 2})
            application.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(application, file_logging=False)
    budgets = []
    with inspect_run(result.run) as trace:
        for event in trace.events():
            if event["kind"] == "model_request":
                request = json.loads(trace.read_text(event["request_ref"]))
                if "output_budget" in request:
                    budgets.append(request["output_budget"])
    assert len(requests) == 1 and budgets
    for budget in budgets:
        assert budget["effective_cap"] is None and budget["wire_cap"] is None
        assert budget["source"] == "cleared"
        assert budget["input_token_reserve"] == 8192
        assert budget["input_reserve_source"] == "input_token_reserve"
    (tmp_path / "budget-evidence.json").write_text(json.dumps(budgets, indent=2))
