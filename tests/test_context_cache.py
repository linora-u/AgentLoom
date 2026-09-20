"""Context-cache behavior at the model wire-adapter seam."""

from __future__ import annotations

import pytest
from agentloom.adapters.litellm import (
    AnthropicMessagesModelTurnAdapter,
    OpenAIChatModelTurnAdapter,
)
from agentloom.runtime.model_protocol import MessageItem, ModelTurnRequest


def _chat_response() -> dict:
    return {
        "id": "response-1",
        "choices": [{"message": {"content": "done", "tool_calls": []}}],
        "usage": {},
    }


@pytest.mark.parametrize(
    ("adapter_type", "payload_key", "block_type"),
    [
        (OpenAIChatModelTurnAdapter, "messages", "text"),
        (AnthropicMessagesModelTurnAdapter, "messages", "text"),
    ],
)
def test_context_cache_marks_static_system_content(
    adapter_type,
    payload_key: str,
    block_type: str,
) -> None:
    captured: dict = {}

    def transport(**request):
        captured.update(request)
        return _chat_response()

    adapter = adapter_type(transport=transport, context_cache=True)
    adapter.turn(
        ModelTurnRequest(
            model="opaque-model",
            items=(MessageItem(role="system", text="Static instructions"),),
        )
    )

    content = captured[payload_key][0]["content"]
    assert content == [
        {
            "type": block_type,
            "text": "Static instructions",
            "cache_control": {"type": "ephemeral"},
        }
    ]


@pytest.mark.parametrize(
    ("adapter_type", "payload_key", "block_type"),
    [
        (OpenAIChatModelTurnAdapter, "messages", "text"),
        (AnthropicMessagesModelTurnAdapter, "messages", "text"),
    ],
)
def test_context_cache_boundary_keeps_dynamic_suffix_uncached(
    adapter_type,
    payload_key: str,
    block_type: str,
) -> None:
    captured: dict = {}

    def transport(**request):
        captured.update(request)
        return _chat_response()

    adapter = adapter_type(
        transport=transport,
        context_cache=True,
        system_prompt_boundary="<!-- dynamic -->",
    )
    adapter.turn(
        ModelTurnRequest(
            model="opaque-model",
            items=(
                MessageItem(
                    role="system",
                    text="Static instructions<!-- dynamic -->Current task",
                ),
            ),
        )
    )

    assert captured[payload_key][0]["content"] == [
        {
            "type": block_type,
            "text": "Static instructions",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": block_type, "text": "Current task"},
    ]


def test_disabled_context_cache_preserves_plain_system_text() -> None:
    captured: dict = {}

    def transport(**request):
        captured.update(request)
        return _chat_response()

    OpenAIChatModelTurnAdapter(
        transport=transport,
        context_cache=False,
    ).turn(
        ModelTurnRequest(
            model="opaque-model",
            items=(MessageItem(role="system", text="Plain system text"),),
        )
    )

    assert captured["messages"][0] == {
        "role": "system",
        "content": "Plain system text",
    }


def test_context_cache_does_not_depend_on_model_name() -> None:
    captured_models: list[str] = []

    def transport(**request):
        captured_models.append(request["model"])
        assert request["messages"][0]["content"][0]["cache_control"] == {
            "type": "ephemeral"
        }
        return _chat_response()

    adapter = OpenAIChatModelTurnAdapter(
        transport=transport,
        context_cache=True,
    )
    for model in (
        "anthropic/MiniMax-M2.7",
        "cohere/command-r-plus",
        "opaque/private-model",
    ):
        adapter.turn(
            ModelTurnRequest(
                model=model,
                items=(MessageItem(role="system", text="System"),),
            )
        )

    assert captured_models == [
        "anthropic/MiniMax-M2.7",
        "cohere/command-r-plus",
        "opaque/private-model",
    ]
