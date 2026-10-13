"""Output-budget contracts observed after the real Pi SDK builds HTTP requests."""
from __future__ import annotations

import json
import pytest
import yaml
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from test_application import model_service, project


@pytest.mark.parametrize("responses", [False, True])
def test_omitted_output_cap_stays_omitted_on_wire(tmp_path, responses):
    with model_service(responses=responses) as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        config = yaml.safe_load(path.read_text())
        config["model"]["test"].pop("max_output_tokens")
        config["model"]["test"]["context_window"] = 500000
        if responses:
            config["model"]["test"]["adapter"] = "openai_responses"
        path.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    assert len(requests) == 1
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1])


def test_local_null_override_removes_inherited_cap_on_wire(tmp_path):
    with model_service(responses=True) as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        config = yaml.safe_load(path.read_text())
        config["model"]["test"].update(adapter="openai_responses", max_output_tokens=32768, context_window=500000)
        path.write_text(yaml.safe_dump(config))
        (app.parent.parent / "config").mkdir()
        (app.parent.parent / "config/model.local.yaml").write_text("model:\n  test:\n    max_output_tokens: null\n")
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    assert "max_output_tokens" not in requests[0][1]


@pytest.mark.parametrize("cap", [None, 32768])
def test_codex_output_cap_intent_reaches_actual_sdk_payload(tmp_path, monkeypatch, cap):
    from tests.pi_test.test_codex_application import _fixture

    app, wire = _fixture(tmp_path, monkeypatch, search="off")
    path = tmp_path / "config/model.yaml"
    profiles = yaml.safe_load(path.read_text())
    profiles["model"]["codex"].update(context_window=500000, max_output_tokens=cap)
    path.write_text(yaml.safe_dump(profiles))
    with bind_config(load_project_config(tmp_path)):
        result = execute_app(app, file_logging=False)
    requests = [json.loads(line) for line in wire.read_text().splitlines()]
    assert result.output and len(requests) == 1
    body = requests[0]["body"]
    if cap is None:
        assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(body)
    else:
        assert body["max_output_tokens"] == cap
        assert "max_tokens" not in body and "max_completion_tokens" not in body


@pytest.mark.parametrize("changes", [
    {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_output_tokens": -1},
    {"max_output_tokens": "32768"},
    {"max_output_tokens": None, "extra_body": {"max_tokens": 32768}},
    {"max_output_tokens": 100, "extra_body": {"max_completion_tokens": 200}},
    {"max_model_output_tokens": 100},
    {"max_output_tokens": 200, "max_model_output_tokens": 100,
     "model_output_capability_source": "https://provider.example/verified-limit"},
])
def test_invalid_or_conflicting_caps_fail_before_http(tmp_path, changes):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        config = yaml.safe_load(path.read_text())
        config["model"]["test"].update(changes)
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(ValueError, match="(output|cap|token)"):
            with bind_config(load_project_config(tmp_path)):
                execute_app(app, file_logging=False)
    assert requests == []


@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("cap", [7, 32768])
def test_explicit_cap_survives_sdk_minimum_and_context_clamping(tmp_path, responses, cap):
    with model_service(responses=responses) as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        config = yaml.safe_load(path.read_text())
        config["model"]["test"].update(max_output_tokens=cap, context_window=500000,
                                          extra_body={"max_completion_tokens": cap})
        if responses:
            config["model"]["test"]["adapter"] = "openai_responses"
        path.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    field = "max_output_tokens" if responses else "max_tokens"
    assert requests[0][1][field] == cap
    assert {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1]) == {field}


@pytest.mark.parametrize("responses,reason", [(False, "length"), (True, "length"), (True, "max_output_tokens")])
def test_provider_truncation_uses_output_validation_without_automatic_budget_increase(tmp_path, responses, reason):
    from agentloom.app.run import ApplicationRunError
    with model_service(responses=responses, finish=reason) as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        config = yaml.safe_load(path.read_text())
        if responses:
            config["model"]["test"]["adapter"] = "openai_responses"
        path.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as captured:
            execute_app(app, file_logging=False)
    assert captured.value.original_error.category == "output_validation"
    assert captured.value.original_error.retryable
    assert len(requests) == 1
    assert requests[0][1]["max_output_tokens" if responses else "max_tokens"] == 100


@pytest.mark.parametrize("cap", [None, 16])
def test_native_compaction_keeps_its_explicit_budget_separate_from_profile_cap(tmp_path, cap):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        config = yaml.safe_load(path.read_text())
        config["model"]["test"].update(context_window=64, max_output_tokens=cap)
        path.write_text(yaml.safe_dump(config))
        config = yaml.safe_load(app.read_text())
        config["runtime_options"]["compaction"] = {"enabled": True, "reserveTokens": 56, "keepRecentTokens": 1}
        app.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    summaries = [payload for _, payload, _ in requests if any(
        message["role"] == "system" and "context summarization assistant" in str(message["content"])
        for message in payload["messages"])]
    ordinary = [payload for _, payload, _ in requests if payload not in summaries]
    assert summaries
    # First-turn prefix summarization uses half of reserveTokens (SDK's
    # explicit internal policy), rather than the profile's ordinary cap.
    assert all(payload["max_tokens"] == 28 for payload in summaries)
    if cap is None:
        assert all("max_tokens" not in payload for payload in ordinary)
    else:
        assert all(payload["max_tokens"] == 16 for payload in ordinary)
