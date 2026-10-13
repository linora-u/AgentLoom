"""Public profile/binding and smol Application contracts at actual HTTP transport."""
from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.config.llm_config import LLMConfig, LlmModelTypeSettings
from agentloom.execution.model_protocol import MessageItem
from agentloom.integrations.litellm.model_binding import ModelProfileOverlay, resolve_litellm_model_turn_binding
from test_application import project


@contextmanager
def json_model_service(*, smol=False, finish="stop", incomplete=None, first_tool=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, request))
            prefix = first_tool and len(requests) == 1
            if self.path.endswith("/responses"):
                response = {"id": "resp_fixture", "object": "response", "created_at": 0,
                    "model": request["model"], "status": "incomplete" if incomplete else "completed",
                    "incomplete_details": {"reason": incomplete} if incomplete else None,
                    "output": [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Budget answer", "annotations": []}]}],
                    "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}}
                if prefix:
                    response.update(status="completed", incomplete_details=None, output=[{
                        "type": "function_call", "id": "fc_probe", "call_id": "call_probe", "status": "completed",
                        "name": "budget_probe", "arguments": '{"value":"reported first turn"}'}])
            elif self.path.endswith("/messages"):
                response = {"id": "msg_fixture", "type": "message", "model": request["model"], "role": "assistant",
                    "content": [{"type": "text", "text": "Budget answer"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 3}}
            else:
                message = {"role": "assistant", "content": "Budget answer"}
                if smol:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_final",
                        "type": "function", "function": {"name": "final_answer", "arguments": '{"answer":"Budget answer"}'}}]}
                if prefix:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_probe",
                        "type": "function", "function": {"name": "budget_probe", "arguments": '{"value":"reported first turn"}'}}]}
                response = {"id": "chat_fixture", "object": "chat.completion", "created": 0,
                    "model": request["model"], "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if prefix or smol and finish == "stop" else finish}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
        thread.join()


def profile(url, **changes):
    return LlmModelTypeSettings(model="openai/budget-fixture", adapter="openai_chat", base_url=url,
        api_key="fixture-secret", num_retries=0, context_window=500000, **changes)


@pytest.mark.parametrize("adapter", ["openai_chat", "openai_responses"])
def test_tool_schema_budget_named_properties_reach_actual_sdk_http_unchanged(adapter):
    from agentloom.execution.model_protocol import ToolDefinition

    schema = {"type": "object", "properties": {
        "max_tokens": {"type": "integer"}, "max_completion_tokens": {"type": "null"},
        "max_output_tokens": {"type": "number"}}, "required": ["max_tokens"]}
    with json_model_service() as (url, requests):
        settings = profile(url, max_output_tokens=32768).model_copy(update={"adapter": adapter})
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": settings}))
        result = binding.turn(items=(MessageItem(role="user", text="Read the tool schema"),),
                              tools=(ToolDefinition("budget_data", "Business data", schema),))
    assert result.items[0].text == "Budget answer" and len(requests) == 1
    body = requests[0][1]
    tool = body["tools"][0]
    assert (tool["parameters"] if adapter == "openai_responses" else tool["function"]["parameters"]) == schema
    assert body["max_output_tokens" if adapter == "openai_responses" else "max_tokens"] == 32768


def test_response_schema_budget_named_properties_are_data_at_actual_sdk_http():
    schema = {"type": "object", "properties": {"max_output_tokens": {"type": "integer"}},
              "required": ["max_output_tokens"], "additionalProperties": False}
    formatting = {"type": "json_schema", "json_schema": {"name": "business_data", "strict": True, "schema": schema}}
    with json_model_service() as (url, requests):
        settings = profile(url, max_output_tokens=32768, extra_completion_params={"response_format": formatting})
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": settings}))
        result = binding.turn(items=(MessageItem(role="user", text="Return business data"),))
    assert result.items[0].text == "Budget answer" and len(requests) == 1
    assert requests[0][1]["response_format"] == formatting
    assert requests[0][1]["max_tokens"] == 32768


@pytest.mark.parametrize("adapter", ["openai_chat", "openai_responses"])
def test_null_overlay_clears_explicit_cap_without_clearing_input_reserve(tmp_path, adapter):
    with json_model_service() as (url, requests):
        base = profile(url, max_output_tokens=32768).model_copy(update={"adapter": adapter})
        overlay = ModelProfileOverlay(max_output_tokens=None)
        catalog = LLMConfig(default_model_type="test", models={"test": base})
        binding = resolve_litellm_model_turn_binding("test", llm_config=catalog, profile_overlay=overlay)
        result = binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert result.items[0].text == "Budget answer"
    assert binding.max_output_tokens is None
    assert binding.input_token_limit == 467232
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1])


def test_smol_application_omitted_cap_stays_omitted_after_litellm_global_defaults(tmp_path):
    import litellm
    prior = litellm.OpenAIConfig.get_config().get("max_tokens")
    litellm.OpenAIConfig(max_tokens=999)
    try:
        with json_model_service(smol=True) as (url, requests):
            app = project(tmp_path, url)
            path = tmp_path / "config/model.yaml"
            config = yaml.safe_load(path.read_text())
            config["model"]["test"].pop("max_output_tokens")
            config["model"]["test"]["context_window"] = 500000
            path.write_text(yaml.safe_dump(config))
            config = yaml.safe_load(app.read_text())
            config.update(agent_runtime="smolagents", runtime_options={"smart_summary": False, "max_steps": 2})
            app.write_text(yaml.safe_dump(config))
            with bind_config(load_project_config(tmp_path)):
                result = execute_app(app, file_logging=False)
    finally:
        litellm.OpenAIConfig.max_tokens = prior
    assert result.output == "Budget answer"
    assert len(requests) == 1
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1])


@pytest.mark.parametrize("adapter,reason", [("openai_chat", "length"), ("openai_responses", "length"), ("openai_responses", "max_output_tokens")])
def test_litellm_truncation_preserves_usage_and_raw_reason(adapter, reason):
    with json_model_service(finish=reason, incomplete=reason if adapter == "openai_responses" else None) as (url, requests):
        settings = profile(url, max_output_tokens=32768).model_copy(update={"adapter": adapter})
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": settings}))
        with pytest.raises(Exception) as captured:
            binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert captured.value.category == "output_validation"
    assert captured.value.raw_reason == reason
    assert captured.value.usage.total_tokens == 15
    assert captured.value.output_budget["wire_cap"] == 32768
    assert len(requests) == 1


@pytest.mark.parametrize("cap", [None, 32768])
def test_direct_construction_and_serialization_preserve_cap_intent_at_http(cap):
    with json_model_service() as (url, requests):
        original = profile(url, max_output_tokens=cap)
        restored = LlmModelTypeSettings.model_validate_json(original.model_dump_json())
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": restored}))
        assert binding.turn(items=(MessageItem(role="user", text="Answer"),)).items[0].text == "Budget answer"
    assert requests[0][1].get("max_tokens") == cap
    if cap is None:
        assert "max_tokens" not in requests[0][1]
        assert restored.input_token_limit == 483616


def test_required_cap_adapter_fails_before_http_when_capability_is_unknown():
    with json_model_service() as (url, requests):
        with pytest.raises(ValueError, match="requires max_output_tokens"):
            LlmModelTypeSettings(model="anthropic/fixture", adapter="anthropic_messages", base_url=url)
    assert requests == []


def test_required_cap_adapter_uses_only_documented_model_output_capability():
    with json_model_service() as (url, requests):
        settings = LlmModelTypeSettings(model="anthropic/fixture", adapter="anthropic_messages", base_url=url.removesuffix("/v1"),
            api_key="fixture-secret", num_retries=0, max_model_output_tokens=8192,
            model_output_capability_source="https://provider.example/fixture-model-output-limit")
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": settings}))
        result = binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert result.items[0].text == "Budget answer"
    assert requests[0][0] == "/v1/messages"
    assert requests[0][1]["max_tokens"] == 8192
    assert settings.output_cap_source == "verified_model_capability"


def test_chatgpt_subscription_rejects_unsupported_explicit_cap_before_network():
    with json_model_service() as (url, requests):
        with pytest.raises(ValueError, match="does not support"):
            LlmModelTypeSettings(model="fixture", adapter="openai_chatgpt_responses", base_url=url, max_output_tokens=100)
    assert requests == []


@pytest.mark.parametrize("mode,limit", [("initial_null", 483616), ("clear_cap", 467232), ("legacy", 256)])
def test_smol_compatibility_builder_and_cache_keep_null_override(tmp_path, mode, limit):
    from agentloom.runtimes.smolagents.models.model_manager import ModelConfigBuilder, ModelConfigOverlay, ModelManager
    from agentloom.runtimes.smolagents.models.model_types import ModelType
    with json_model_service(smol=True) as (url, requests):
        project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        catalog = yaml.safe_load(path.read_text())
        catalog["model"]["test"].update(context_window=500000, max_output_tokens=32768)
        if mode == "initial_null":
            catalog["model"]["test"]["max_output_tokens"] = None
        elif mode == "legacy":
            catalog["model"]["test"].pop("max_output_tokens")
            catalog["model"]["test"].pop("context_window")
            catalog["model"]["test"]["max_tokens"] = 512
        path.write_text(yaml.safe_dump(catalog))
        builder = ModelConfigBuilder()
        if mode != "initial_null":
            builder.apply_overlay(ModelConfigOverlay(max_output_tokens=None))
        with bind_config(load_project_config(tmp_path)):
            manager = ModelManager()
            model = manager.get_smolagents_model(ModelType("test"), model_builder=builder)
            second = manager.get_smolagents_model(ModelType("test"), model_builder=builder)
            assert model is second
            model.binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1])
    assert model.binding.max_output_tokens is None
    assert model.binding.input_token_limit == limit


def test_explicit_reserve_overlay_records_its_source_at_final_wire():
    with json_model_service() as (url, requests):
        catalog = LLMConfig(models={"test": profile(url, max_output_tokens=32768)})
        binding = resolve_litellm_model_turn_binding("test", llm_config=catalog,
            profile_overlay=ModelProfileOverlay(max_output_tokens=None, input_token_reserve=8192))
        binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert binding.input_token_reserve == 8192
    assert binding.input_token_limit == 491808
    assert binding.input_reserve_source == "input_token_reserve"
    assert "max_tokens" not in requests[0][1]


def test_explicit_cap_retains_sdk_protocol_field_for_reasoning_chat_models():
    with json_model_service() as (url, requests):
        settings = LlmModelTypeSettings(model="openai/gpt-5", adapter="openai_chat", base_url=url,
            api_key="fixture-secret", temperature=1, num_retries=0, max_output_tokens=32768)
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": settings}))
        result = binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert result.items[0].text == "Budget answer"
    assert requests[0][1]["max_completion_tokens"] == 32768
    assert "max_tokens" not in requests[0][1]


def test_budget_policy_preserves_environment_proxy_routing(monkeypatch):
    with json_model_service() as (proxy_url, requests):
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, proxy_url.removesuffix("/v1"))
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "")
        settings = profile("http://budget-endpoint.invalid/v1", max_output_tokens=None)
        binding = resolve_litellm_model_turn_binding("test", llm_config=LLMConfig(models={"test": settings}))
        result = binding.turn(items=(MessageItem(role="user", text="Answer"),))
    assert result.items[0].text == "Budget answer"
    assert requests[0][0] == "http://budget-endpoint.invalid/v1/chat/completions"
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1])


def test_direct_adapter_truncation_keeps_wire_budget_evidence():
    from agentloom.execution.model_protocol import ModelOutputTruncated, ModelTurnRequest
    from agentloom.integrations.litellm.model_turn import create_model_turn_adapter

    with json_model_service(finish="length") as (url, requests):
        adapter = create_model_turn_adapter("openai_chat")
        request = ModelTurnRequest(model="openai/budget-fixture", items=(MessageItem(role="user", text="Answer"),),
            options={"api_base": url, "api_key": "fixture-secret", "num_retries": 0, "max_tokens": 32768})
        with pytest.raises(ModelOutputTruncated) as captured:
            adapter.turn(request)
    assert captured.value.raw_reason == "length"
    assert captured.value.output_budget["wire_cap"] == 32768
    assert requests[0][1]["max_tokens"] == 32768


@pytest.mark.parametrize("adapter,reason", [("openai_chat", "length"), ("openai_responses", "length"), ("openai_responses", "max_output_tokens")])
def test_smol_application_truncation_is_retryable_output_validation(tmp_path, adapter, reason):
    from agentloom.app.run import ApplicationRunError

    with json_model_service(smol=True, finish=reason, incomplete=reason if adapter == "openai_responses" else None) as (url, requests):
        app = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        catalog = yaml.safe_load(path.read_text())
        catalog["model"]["test"]["adapter"] = adapter
        path.write_text(yaml.safe_dump(catalog))
        config = yaml.safe_load(app.read_text())
        config.update(agent_runtime="smolagents", runtime_options={"smart_summary": False, "max_steps": 2})
        app.write_text(yaml.safe_dump(config))
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as captured:
            execute_app(app, file_logging=False)
    assert captured.value.original_error.category == "output_validation"
    assert captured.value.original_error.retryable
    assert len(requests) == 1
    assert requests[0][1]["max_output_tokens" if adapter == "openai_responses" else "max_tokens"] == 100


def budget_probe(value: str) -> str:
    """Return the actual model's tool argument before its next turn.

    Args:
        value: Text to return to the model.
    """
    return value


@pytest.mark.parametrize("first_tool", [False, True])
@pytest.mark.parametrize("adapter,reason", [("openai_chat", "length"),
    ("openai_responses", "length"), ("openai_responses", "max_output_tokens")])
def test_smol_truncated_worker_receipt_keeps_reported_provider_usage(tmp_path, adapter, reason, first_tool):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem

    with json_model_service(smol=True, finish=reason, incomplete=reason if adapter == "openai_responses" else None,
                            first_tool=first_tool) as (url, requests):
        application = project(tmp_path, url)
        path = tmp_path / "config/model.yaml"
        profiles = yaml.safe_load(path.read_text())
        profiles["model"]["test"]["adapter"] = adapter
        path.write_text(yaml.safe_dump(profiles))
        definition = yaml.safe_load(application.read_text())
        definition.update(agent_runtime="smolagents", runtime_options={"smart_summary": False, "max_steps": 2})
        if first_tool:
            definition["tools"] = [{"name": "budget_probe", "module": __name__, "function": "budget_probe"}]
        application.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)), Application.open(
                application.parent.parent, project_root=tmp_path, file_logging=False) as app:
            outcome = app.worker("workflows/root.yaml").invoke(WorkItem("truncate", {}))
    assert outcome.status == "failed" and outcome.error.category == "output_validation"
    count = 2 if first_tool else 1
    assert outcome.error.retryable is True and len(requests) == count
    assert outcome.usage["input_tokens"] == 12 * count
    assert outcome.usage["output_tokens"] == 3 * count
    assert outcome.usage["total_tokens"] == 15 * count


def test_smol_stop_continuation_counts_each_new_provider_turn_once(tmp_path):
    import sys
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem

    with json_model_service(smol=True) as (url, requests):
        application = project(tmp_path, url)
        hook = tmp_path / "stop_once.py"
        marker = tmp_path / "stop_count"
        hook.write_text("import json,sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
                        f"p=Path({str(marker)!r})\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n"
                        "print(json.dumps({'decision':'block' if n==0 else 'allow','reason':'Continue once.'}))\n")
        definition = yaml.safe_load(application.read_text())
        definition.update(agent_runtime="smolagents", runtime_options={"smart_summary": False, "max_steps": 2},
            hooks={"Stop": [{"id": "usage-continuation", "command": f"{sys.executable} {hook}"}]})
        application.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)), Application.open(
                application.parent.parent, project_root=tmp_path, file_logging=False) as app:
            outcome = app.worker("workflows/root.yaml").invoke(WorkItem("continue", {}))
    assert outcome.status == "success" and len(requests) == 2
    assert outcome.usage["input_tokens"] == 24
    assert outcome.usage["output_tokens"] == 6
    assert outcome.usage["total_tokens"] == 30


def test_legacy_yaml_budgets_and_null_overlay_migrate_without_reinterpreting_legacy_input():
    with json_model_service() as (url, requests):
        catalog = LLMConfig.from_dict({"model": {"test": {"adapter": "openai_chat", "model": "openai/budget-fixture",
            "base_url": url, "api_key": "fixture-secret", "num_retries": 0, "max_tokens": 512}}})
        legacy = catalog.models["test"]
        assert legacy.context_window == legacy.max_output_tokens == legacy.input_token_limit == 512
        assert legacy.output_cap_source == "legacy_max_tokens"
        resolve_litellm_model_turn_binding("test", llm_config=catalog).turn(items=(MessageItem(role="user", text="Answer"),))
        migrated = resolve_litellm_model_turn_binding("test", llm_config=catalog,
            profile_overlay=ModelProfileOverlay(max_output_tokens=None))
        migrated.turn(items=(MessageItem(role="user", text="Answer"),))
    assert requests[0][1]["max_tokens"] == 512
    assert "max_tokens" not in requests[1][1]
    assert migrated.context_window == 512
    assert migrated.input_token_limit == 256
    assert migrated.output_cap_source == "cleared"
