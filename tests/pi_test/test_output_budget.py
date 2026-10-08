"""An omitted Codex output budget must survive profile resolution."""

import pytest

from agentloom.config.llm_config import LLMConfig
from agentloom.config.defaults import DEFAULT_MAX_OUTPUT_TOKENS
from agentloom.integrations.litellm.model_binding import ModelProfileOverlay


@pytest.mark.parametrize("explicit", [False, True])
def test_codex_omitted_or_null_output_budget_is_native(explicit):
    profile = {"model": "gpt-6-luna", "adapter": "openai_codex_responses", "context_window": 500000}
    if explicit:
        profile["max_output_tokens"] = None
    settings = LLMConfig.from_dict({"model": {"codex": profile, "summary": profile}}).models["codex"]
    assert settings.max_output_tokens is None
    assert settings.input_token_limit == 500000
    overlaid = ModelProfileOverlay(context_window=400000).apply(settings)
    assert overlaid.max_output_tokens is None and overlaid.input_token_limit == 400000


def test_other_adapters_keep_default_and_explicit_codex_cap_still_works():
    raw = {"model": {
        "summary": {"model": "gpt-6-luna", "adapter": "openai_chat"},
        "chat": {"model": "gpt-6-luna", "adapter": "openai_chat"},
        "codex": {"model": "gpt-6-luna", "adapter": "openai_codex_responses", "max_output_tokens": 65536},
    }}
    profiles = LLMConfig.from_dict(raw).models
    assert profiles["chat"].max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert profiles["codex"].max_output_tokens == 65536


def test_non_codex_null_preserves_existing_default():
    profile = {"model": "fixture", "adapter": "openai_chat", "max_output_tokens": None}
    settings = LLMConfig.from_dict({"model": {"summary": profile}}).models["summary"]
    assert settings.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
