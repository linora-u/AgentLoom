from types import SimpleNamespace

import pytest
from smolagents import LiteLLMModel

from src.lib.goal import GoalBudgetLimitedError, GoalCompleteError, GoalState
from src.lib.goal.provider import GoalStateProvider, bind_goal_state_provider
from src.lib.smolagents.memory.context_compression import (
    InternalChatMessage,
    summarize_conversation,
)
from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
from src.trace import bind_local_run


def _model():
    model = object.__new__(LiteLLMModelV2)
    model._normalize_and_validate_tool_calls = lambda *_args: None
    return model


def _provider(budget=100):
    return GoalStateProvider(
        GoalState.create(
            objective="Ship.",
            objective_fingerprint="abc",
            token_budget=budget,
        )
    )


def test_model_response_usage_is_charged_once_and_next_request_is_fenced(monkeypatch):
    calls = 0

    def fake_generate(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            token_usage=SimpleNamespace(input_tokens=80, output_tokens=25)
        )

    monkeypatch.setattr(LiteLLMModel, "generate", fake_generate)
    provider = _provider()
    model = _model()

    with bind_goal_state_provider(provider):
        model.generate([])
        with pytest.raises(GoalBudgetLimitedError):
            model.generate([])

    assert calls == 1
    assert provider.snapshot().used_tokens == 105
    assert provider.snapshot().goal_started is True


def test_worker_and_supervisor_responses_aggregate_through_shared_provider(monkeypatch):
    def fake_generate(self, *args, **kwargs):
        return SimpleNamespace(
            token_usage=SimpleNamespace(input_tokens=10, output_tokens=5)
        )

    monkeypatch.setattr(LiteLLMModel, "generate", fake_generate)
    provider = _provider(budget=None)

    with bind_goal_state_provider(provider):
        _model().generate([])
        _model().generate([])

    assert provider.snapshot().prompt_tokens == 20
    assert provider.snapshot().completion_tokens == 10


def test_completion_allows_one_root_final_answer_settlement_request(monkeypatch):
    observed_tools: list[list[str]] = []

    def fake_generate(self, *args, **kwargs):
        observed_tools.append(
            [tool.name for tool in kwargs.get("tools_to_call_from") or []]
        )
        return SimpleNamespace(
            token_usage=SimpleNamespace(input_tokens=10, output_tokens=5)
        )

    monkeypatch.setattr(LiteLLMModel, "generate", fake_generate)
    provider = _provider(budget=None)
    provider.complete("Delivered.", settlement_run_id="root")
    tools = [
        SimpleNamespace(name="get_goal"),
        SimpleNamespace(name="final_answer"),
    ]

    with bind_goal_state_provider(provider):
        with bind_local_run("worker"):
            with pytest.raises(GoalCompleteError):
                _model().generate([], tools_to_call_from=tools)
        with bind_local_run("root"):
            _model().generate([], tools_to_call_from=tools)
            with pytest.raises(GoalCompleteError):
                _model().generate([], tools_to_call_from=tools)

    assert observed_tools == [["final_answer"]]
    assert provider.snapshot().status == "complete"
    assert provider.snapshot().used_tokens == 15


def test_completion_skips_planning_before_claiming_final_settlement(monkeypatch):
    observed_tools: list[list[str]] = []

    def fake_generate(self, *args, **kwargs):
        observed_tools.append(
            [tool.name for tool in kwargs.get("tools_to_call_from") or []]
        )
        return SimpleNamespace(
            token_usage=SimpleNamespace(input_tokens=10, output_tokens=5)
        )

    monkeypatch.setattr(LiteLLMModel, "generate", fake_generate)
    provider = _provider(budget=None)
    provider.complete("Delivered.", settlement_run_id="root")
    tools = [
        SimpleNamespace(name="get_goal"),
        SimpleNamespace(name="final_answer"),
    ]

    with bind_goal_state_provider(provider), bind_local_run("root"):
        plan = _model().generate([], stop_sequences=["<end_plan>"])
        _model().generate([], tools_to_call_from=tools)

    assert plan.content == "Goal is complete. Skip planning and deliver the final answer now."
    assert plan.token_usage.input_tokens == 0
    assert observed_tools == [["final_answer"]]


def test_max_steps_prose_fallback_can_claim_final_settlement(monkeypatch):
    observed_tools: list[object] = []

    def fake_generate(self, *args, **kwargs):
        observed_tools.append(kwargs.get("tools_to_call_from"))
        return SimpleNamespace(
            token_usage=SimpleNamespace(input_tokens=10, output_tokens=5)
        )

    monkeypatch.setattr(LiteLLMModel, "generate", fake_generate)
    provider = _provider(budget=None)
    provider.complete("Delivered.", settlement_run_id="root")

    with bind_goal_state_provider(provider), bind_local_run("root"):
        _model().generate([])

    assert observed_tools == [None]


def test_budget_crossing_does_not_grant_completion_settlement(monkeypatch):
    calls = 0

    def fake_generate(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(token_usage=None)

    monkeypatch.setattr(LiteLLMModel, "generate", fake_generate)
    provider = _provider(budget=100)
    provider.record_usage(prompt_tokens=90, completion_tokens=20)
    provider.complete("Delivered.", settlement_run_id="root")

    with bind_goal_state_provider(provider), bind_local_run("root"):
        with pytest.raises(GoalBudgetLimitedError):
            _model().generate(
                [],
                tools_to_call_from=[SimpleNamespace(name="final_answer")],
            )

    assert calls == 0


def test_smart_summary_cannot_consume_completion_settlement(monkeypatch):
    from smolagents.models import ChatMessage, MessageRole

    from src.lib.smolagents.memory import context_compression

    monkeypatch.setattr(
        context_compression.model_manager,
        "get_smolagents_model",
        lambda *_args, **_kwargs: pytest.fail("summary model must not be called"),
    )
    provider = _provider(budget=None)
    provider.complete("Delivered.", settlement_run_id="root")
    messages = [
        InternalChatMessage(ChatMessage(role=MessageRole.USER, content="task")),
        InternalChatMessage(ChatMessage(role=MessageRole.ASSISTANT, content="done")),
    ]

    with bind_goal_state_provider(provider), bind_local_run("root"):
        result = summarize_conversation(messages, "summary-model")

    assert result.error == "Goal completion settlement pending; smart summary skipped"
    assert provider.completion_settlement_pending(local_run_id="root") is True


def test_restored_complete_goal_has_no_final_delivery_allowance():
    completed = _provider(budget=None)
    completed.complete("Delivered.", settlement_run_id="root")
    restored = GoalStateProvider(completed.snapshot())

    with pytest.raises(GoalCompleteError):
        restored.assert_request_allowed(
            local_run_id="root",
            allow_completion_settlement=True,
        )
