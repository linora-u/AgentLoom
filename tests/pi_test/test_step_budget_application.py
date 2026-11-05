"""PI's framework step budget, exercised through the installed SDK and YAML."""

from __future__ import annotations

import json
import sys

import pytest
import yaml
from agentloom.app.application import Application
from agentloom.app.run import ApplicationRunError
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency.batch_models import WorkItem
from agentloom.runtimes.pi.metadata import DEFAULT_MAX_STEPS, validate_options

from tests.pi_test.test_application import model_service, project


def check_value(value: int) -> dict:
    """A nonterminal tool that can return a correctable argument error."""
    if value < 1:
        raise ValueError("value must be positive")
    return {"value": value}


def finish_value(value: int) -> dict:
    """A terminal tool; a correctable error must keep the current Agent alive."""
    return check_value(value)


finish_value._agentloom_terminal_on_success = True


def configured_project(root, url, *, max_steps, terminal=False):
    path = project(root, url)
    definition = yaml.safe_load(path.read_text())
    definition["runtime_options"]["max_steps"] = max_steps
    name = "finish_value" if terminal else "check_value"
    definition["tools"] = [{
        "name": name, "module": "tests.pi_test.test_step_budget_application", "function": name,
    }]
    path.write_text(yaml.safe_dump(definition))
    return path


def events(run):
    return [json.loads(line) for line in (run.run_dir / "audit/runtime_events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "3", None])
def test_invalid_step_budget_rejects_before_http(tmp_path, value):
    with model_service() as (url, requests):
        app = configured_project(tmp_path, url, max_steps=value)
        with bind_config(load_project_config(tmp_path)), pytest.raises(ValueError, match="max_steps"):
            execute_app(app, file_logging=False)
    assert requests == []


def test_default_and_positive_step_budgets():
    assert DEFAULT_MAX_STEPS == 80
    validate_options({})
    validate_options({"max_steps": 1})
    validate_options({"max_steps": 96})


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_repeating_tool_stops_at_exact_budget(tmp_path, limit):
    calls = [[(f"call-{i}", "check_value", {"value": i + 1})] for i in range(limit + 1)]
    with model_service(turns=calls) as (url, requests):
        app = configured_project(tmp_path, url, max_steps=limit)
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as captured:
            execute_app(app, file_logging=False)
    assert captured.value.original_error.category == "max_steps"
    assert captured.value.original_error.retryable is False
    assert len(requests) == limit
    recorded = events(captured.value.run)
    tools = [e for e in recorded if e["kind"] == "tool"]
    assert len(tools) == limit and all(e["details"]["record"]["status"] == "completed" for e in tools)
    terminal = [e["details"] for e in recorded if e["kind"] == "terminal"][-1]
    assert (terminal["state"], terminal["steps"], terminal["max_steps"]) == ("max_steps_error", limit, limit)


@pytest.mark.parametrize("terminal,limit,values", [
    (True, 1, [1]),  # success on the last allowed step
    (True, 10, [1]),  # early successful save
    (True, 2, [0, 1]),  # tool error then same-session correction
    (True, 6, [0, 0, 0, 0, 1]),  # no business-tool four-attempt limit
    (False, 2, [1]),  # tool then natural final answer on the last step
])
def test_success_within_budget_including_last_step(tmp_path, terminal, limit, values):
    name = "finish_value" if terminal else "check_value"
    turns = [[(f"call-{i}", name, {"value": value})] for i, value in enumerate(values)]
    with model_service(turns=turns) as (url, requests):
        app = configured_project(tmp_path, url, max_steps=limit, terminal=terminal)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == ({"value": 1} if terminal else "Pi answer")
    assert len(requests) == len(values) + (not terminal)
    if 0 in values:
        assert "value must be positive" in json.dumps(requests[-1][1])
    assert not any(e["details"].get("state") == "max_steps_error" for e in events(result.run))


def test_multiple_tools_in_one_reply_count_as_one_step(tmp_path):
    turns = [[("first", "check_value", {"value": 1}), ("second", "check_value", {"value": 2})]]
    with model_service(turns=turns) as (url, requests):
        app = configured_project(tmp_path, url, max_steps=1)
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as captured:
            execute_app(app, file_logging=False)
    assert len(requests) == 1
    tools = [e for e in events(captured.value.run) if e["kind"] == "tool"]
    assert len(tools) == 2 and all(e["details"]["record"]["status"] == "completed" for e in tools)


@pytest.mark.parametrize("limit", [1, 2])
def test_output_correction_uses_same_budget(tmp_path, limit):
    with model_service(outputs=["not JSON", '{"ok":true}']) as (url, requests):
        app = project(tmp_path, url)
        definition = yaml.safe_load(app.read_text())
        definition["runtime_options"]["max_steps"] = limit
        definition["output_schema"] = {
            "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
            "additionalProperties": False,
        }
        app.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)):
            if limit == 1:
                with pytest.raises(ApplicationRunError) as captured:
                    execute_app(app, file_logging=False)
                assert captured.value.original_error.category == "max_steps"
            else:
                assert execute_app(app, file_logging=False).output == {"ok": True}
    assert len(requests) == limit


def test_stop_continuations_share_total_step_budget(tmp_path):
    hook = tmp_path / "reject_stop.py"
    hook.write_text('import json\nprint(json.dumps({"decision":"block","reason":"More work required"}))\n')
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        definition = yaml.safe_load(app.read_text())
        definition["runtime_options"].update(max_steps=2, max_stop_attempts=10)
        definition["hooks"] = {"Stop": [{"id": "reject-stop", "command": f"{sys.executable} {hook}"}]}
        app.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as captured:
            execute_app(app, file_logging=False)
    assert captured.value.original_error.category == "max_steps"
    assert len(requests) == 2


def test_independent_worker_invocations_have_independent_budgets(tmp_path):
    with model_service(turns=[[("first", "finish_value", {"value": 1})],
                             [("second", "finish_value", {"value": 1})]]) as (url, requests):
        path = configured_project(tmp_path, url, max_steps=1, terminal=True)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            worker = app.worker("workflows/root.yaml")
            results = [worker.invoke(WorkItem(str(i), {})) for i in range(2)]
    assert all(r.status == "success" and r.output == {"value": 1} for r in results)
    assert len(requests) == 2
    assert results[0].invocation_id != results[1].invocation_id


def test_resume_reuses_remaining_steps_instead_of_resetting_budget(tmp_path):
    from tests.pi_test.test_recovery_application import enable

    calls = [[("first", "check_value", {"value": 1})], None,
             [("resumed", "check_value", {"value": 2})]]
    with model_service(turns=calls, fail_requests={2: 500}) as (url, requests):
        app = configured_project(tmp_path, url, max_steps=2)
        enable(app)
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as first:
                execute_app(app, file_logging=False)
            with pytest.raises(ApplicationRunError) as resumed:
                execute_app(app, resume_task_id=first.value.run.task_id, file_logging=False)
    assert resumed.value.original_error.category == "max_steps"
    assert len(requests) == 3
    assert resumed.value.run.task_id == first.value.run.task_id
    assert resumed.value.run.run_id != first.value.run.run_id
    terminal = [e["details"] for e in events(resumed.value.run) if e["kind"] == "terminal"][-1]
    assert terminal["steps"] == 2 and terminal["max_steps"] == 2


def test_native_compaction_does_not_consume_conversational_step_budget(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        profile = tmp_path / "config/model.yaml"
        config = yaml.safe_load(profile.read_text())
        config["model"]["test"].update(context_window=64, max_output_tokens=16)
        profile.write_text(yaml.safe_dump(config))
        definition = yaml.safe_load(app.read_text())
        definition["runtime_options"].update(max_steps=1, compaction={
            "enabled": True, "reserveTokens": 56, "keepRecentTokens": 1,
        })
        app.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    summaries = [body for _, body, _ in requests if any(
        m["role"] == "system" and "context summarization assistant" in str(m["content"])
        for m in body["messages"]
    )]
    assert summaries and len(requests) > 1
    terminal = [e["details"] for e in events(result.run) if e["kind"] == "terminal"][-1]
    assert terminal["steps"] == 1 and terminal["max_steps"] == 1
