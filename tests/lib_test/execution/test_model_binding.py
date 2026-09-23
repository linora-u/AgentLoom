from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agentloom.configuration.llm_config import LLMConfig, LlmModelTypeSettings
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)
from agentloom.integrations.litellm import model_binding as litellm_binding


class _RecordingAdapter:
    adapter_id = "openai_chat"

    def __init__(self, result: ModelTurnResult | None = None) -> None:
        self.result = result or ModelTurnResult(
            items=(MessageItem(role="assistant", text="done"),)
        )
        self.requests: list[ModelTurnRequest] = []

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        self.requests.append(request)
        return self.result


@pytest.fixture(autouse=True)
def _isolate_binding_state(monkeypatch):
    litellm_binding.clear_litellm_model_turn_binding_cache()
    monkeypatch.setattr(
        litellm_binding,
        "_install_existing_model_governance",
        lambda: None,
    )
    monkeypatch.setattr(
        litellm_binding.GlobalRateLimiterRegistry,
        "get_limiter",
        lambda *_args, **_kwargs: object(),
    )
    yield
    litellm_binding.clear_litellm_model_turn_binding_cache()


def test_binding_sends_one_canonical_turn_with_merged_options() -> None:
    adapter = _RecordingAdapter()
    original_options = {
        "temperature": 0.2,
        "extra_headers": {"X-Profile": "runtime"},
    }
    binding = ModelTurnBinding(
        model_type=" Powerful ",
        model_id="provider/opaque-model",
        adapter=adapter,
        options=original_options,
        max_tokens=32_000,
        context_window=32_000,
        max_output_tokens=4_000,
        input_token_limit=28_000,
        requests_per_minute=30,
        description="primary model",
    )
    original_options["extra_headers"]["X-Profile"] = "mutated"
    tools = (
        ToolDefinition(
            name="lookup",
            description="Look up one value.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        ),
    )

    result = binding.turn(
        items=(MessageItem(role="user", text="look it up"),),
        tools=tools,
        instructions="Use tools.",
        options={"temperature": 0.7, "tool_choice": "required"},
    )

    assert result.items == (MessageItem(role="assistant", text="done"),)
    assert binding.model_type == "powerful"
    assert binding.adapter_id == "openai_chat"
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.model == "provider/opaque-model"
    assert request.items == (MessageItem(role="user", text="look it up"),)
    assert request.tools == tools
    assert request.instructions == "Use tools."
    assert request.options == {
        "temperature": 0.7,
        "extra_headers": {"X-Profile": "runtime"},
        "tool_choice": "required",
    }


def test_binding_rejects_invalid_or_mutable_profile_metadata() -> None:
    adapter = _RecordingAdapter()

    with pytest.raises(ValueError, match="model_type"):
        ModelTurnBinding(model_type=" ", model_id="model", adapter=adapter)
    with pytest.raises(ValueError, match="model_id"):
        ModelTurnBinding(model_type="test", model_id="", adapter=adapter)
    with pytest.raises(ValueError, match="JSON serializable"):
        ModelTurnBinding(
            model_type="test",
            model_id="model",
            adapter=adapter,
            options={"invalid": object()},
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        ModelTurnBinding(
            model_type="test",
            model_id="model",
            adapter=adapter,
            context_window=1_000,
            max_output_tokens=1_001,
        )


def test_binding_repr_does_not_expose_transport_options_or_adapter() -> None:
    binding = ModelTurnBinding(
        model_type="test",
        model_id="provider/model",
        adapter=_RecordingAdapter(),
        options={"api_key": "sensitive-key"},
    )

    rendered = repr(binding)

    assert "sensitive-key" not in rendered
    assert "_RecordingAdapter" not in rendered
    assert "provider/model" in rendered


@pytest.mark.parametrize(
    ("adapter_id", "output_token_key"),
    [
        ("openai_chat", "max_tokens"),
        ("openai_responses", "max_output_tokens"),
        ("anthropic_messages", "max_tokens"),
    ],
)
def test_explicit_litellm_factory_preserves_resolved_profile(
    monkeypatch,
    adapter_id: str,
    output_token_key: str,
) -> None:
    created: list[tuple[str, dict[str, Any]]] = []

    def adapter_factory(selected: str, **kwargs: Any) -> _RecordingAdapter:
        created.append((selected, kwargs))
        adapter = _RecordingAdapter()
        adapter.adapter_id = selected
        return adapter

    settings = LlmModelTypeSettings(
        model="provider/opaque-model",
        adapter=adapter_id,
        base_url="https://models.example.invalid",
        api_key="secret",
        temperature=0.25,
        max_tokens=64_000,
        context_window=64_000,
        max_output_tokens=8_000,
        input_token_limit=56_000,
        timeout=45,
        num_retries=4,
        retry_delay=1.5,
        max_retry_delay=20.0,
        extra_headers={"X-Model": "selected"},
        context_cache=True,
        system_prompt_boundary="<dynamic>",
        description="resolved profile",
        requests_per_minute=17,
        extra_completion_params={"reasoning_effort": "high"},
    )

    binding = litellm_binding.build_litellm_model_turn_binding(
        " Reasoner ",
        settings,
        adapter_factory=adapter_factory,
    )

    assert created == [
        (
            adapter_id,
            {
                "context_cache": True,
                "system_prompt_boundary": "<dynamic>",
            },
        )
    ]
    assert binding.model_type == "reasoner"
    assert binding.model_id == "provider/opaque-model"
    assert binding.adapter_id == adapter_id
    assert binding.max_tokens == 64_000
    assert binding.context_window == 64_000
    assert binding.max_output_tokens == 8_000
    assert binding.input_token_limit == 56_000
    assert binding.requests_per_minute == 17
    assert binding.description == "resolved profile"
    assert binding.options[output_token_key] == 8_000
    other_token_key = (
        "max_tokens" if output_token_key == "max_output_tokens" else "max_output_tokens"
    )
    assert other_token_key not in binding.options
    assert binding.options == {
        output_token_key: 8_000,
        "temperature": 0.25,
        "timeout": 45,
        "num_retries": 4,
        "retry_delay": 1.5,
        "max_retry_delay": 20.0,
        "_agent_loom_model_type": "reasoner",
        "api_base": "https://models.example.invalid",
        "api_key": "secret",
        "extra_headers": {"X-Model": "selected"},
        "reasoning_effort": "high",
    }


def test_config_resolver_uses_existing_default_selection_and_content_cache(
    monkeypatch,
) -> None:
    config = LLMConfig.from_dict(
        {
            "model": {
                "default_model_type": "test",
                "test": {
                    "model": "provider/model",
                    "adapter": "openai_chat",
                    "extra_headers": {"X-Revision": "one"},
                },
                "summary": {
                    "model": "provider/summary",
                    "adapter": "openai_chat",
                },
            }
        }
    )
    monkeypatch.setattr(litellm_binding, "C", SimpleNamespace(llm=config))
    created: list[str] = []

    def adapter_factory(selected: str, **_kwargs: Any) -> _RecordingAdapter:
        created.append(selected)
        return _RecordingAdapter()

    first = litellm_binding.resolve_litellm_model_turn_binding(
        None,
        adapter_factory=adapter_factory,
    )
    cached = litellm_binding.resolve_litellm_model_turn_binding(
        "test",
        adapter_factory=adapter_factory,
    )
    config.models["test"].extra_headers["X-Revision"] = "two"
    refreshed = litellm_binding.resolve_litellm_model_turn_binding(
        "test",
        adapter_factory=adapter_factory,
    )

    assert first is cached
    assert refreshed is not cached
    assert first.model_type == "test"
    assert first.model_id == "provider/model"
    assert first.options["extra_headers"] == {"X-Revision": "one"}
    assert refreshed.options["extra_headers"] == {"X-Revision": "two"}
    assert created == ["openai_chat", "openai_chat"]


def test_config_resolver_applies_runtime_neutral_profile_overlay(
    monkeypatch,
) -> None:
    config = LLMConfig.from_dict(
        {
            "model": {
                "default_model_type": "summary",
                "summary": {
                    "model": "provider/summary",
                    "adapter": "openai_responses",
                    "context_window": 32_000,
                    "max_output_tokens": 4_000,
                    "num_retries": 9,
                    "retry_delay": 3.0,
                    "max_retry_delay": 30.0,
                    "requests_per_minute": 19,
                },
            }
        }
    )
    monkeypatch.setattr(litellm_binding, "C", SimpleNamespace(llm=config))
    limiter_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        litellm_binding.GlobalRateLimiterRegistry,
        "get_limiter",
        lambda model_type, rpm: limiter_calls.append((model_type, rpm)),
    )
    adapter = _RecordingAdapter()
    adapter.adapter_id = "openai_responses"

    binding = litellm_binding.resolve_litellm_model_turn_binding(
        "summary",
        profile_overlay=litellm_binding.ModelProfileOverlay(
            num_retries=0,
            retry_delay=0.0,
            max_retry_delay=0.0,
            context_window=40_000,
            max_output_tokens=2_000,
        ),
        adapter_factory=lambda _selected, **_kwargs: adapter,
    )

    assert binding.model_id == "provider/summary"
    assert binding.adapter is adapter
    assert binding.context_window == 40_000
    assert binding.max_output_tokens == 2_000
    assert binding.input_token_limit == 38_000
    assert binding.requests_per_minute == 19
    assert binding.options["num_retries"] == 0
    assert binding.options["retry_delay"] == 0.0
    assert binding.options["max_retry_delay"] == 0.0
    assert "extra_headers" not in binding.options
    assert limiter_calls == [("summary", 19)]


def test_config_resolver_propagates_unknown_model_type_without_fallback(
    monkeypatch,
) -> None:
    config = LLMConfig.from_dict(
        {
            "model": {
                "default_model_type": "test",
                "test": {
                    "model": "provider/model",
                    "adapter": "openai_chat",
                },
                "summary": {
                    "model": "provider/summary",
                    "adapter": "openai_chat",
                },
            }
        }
    )
    monkeypatch.setattr(litellm_binding, "C", SimpleNamespace(llm=config))

    with pytest.raises(ValueError, match="missing.*not defined"):
        litellm_binding.resolve_litellm_model_turn_binding("missing")


def test_adapter_factory_failure_is_not_retried_with_another_protocol(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def failing_factory(selected: str, **_kwargs: Any) -> _RecordingAdapter:
        calls.append(selected)
        raise RuntimeError("selected adapter unavailable")

    settings = LlmModelTypeSettings(
        model="provider/model",
        adapter="openai_responses",
    )

    with pytest.raises(RuntimeError, match="selected adapter unavailable"):
        litellm_binding.build_litellm_model_turn_binding(
            "test",
            settings,
            adapter_factory=failing_factory,
        )

    assert calls == ["openai_responses"]
