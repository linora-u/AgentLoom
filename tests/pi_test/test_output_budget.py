"""Output-cap intent survives direct settings, YAML, patches and serialization."""

import pytest
from agentloom.config.defaults import DEFAULT_MAX_OUTPUT_TOKENS
from agentloom.config.llm_config import LLMConfig, LlmModelTypeSettings
from agentloom.integrations.litellm.model_binding import ModelProfileOverlay


@pytest.mark.parametrize("explicit", [False, True])
def test_codex_omitted_or_null_output_budget_is_native(explicit):
    profile = {"model": "gpt-6-luna", "adapter": "openai_codex_responses", "context_window": 500000}
    if explicit:
        profile["max_output_tokens"] = None
    settings = LLMConfig.from_dict({"model": {"codex": profile, "summary": profile}}).models["codex"]
    assert settings.max_output_tokens is None
    assert settings.input_token_limit == 483616
    overlaid = ModelProfileOverlay(context_window=400000).apply(settings)
    assert overlaid.max_output_tokens is None and overlaid.input_token_limit == 383616


def test_all_optional_cap_adapters_omit_default_and_explicit_codex_cap_still_works():
    raw = {"model": {
        "summary": {"model": "gpt-6-luna", "adapter": "openai_chat"},
        "chat": {"model": "gpt-6-luna", "adapter": "openai_chat"},
        "codex": {"model": "gpt-6-luna", "adapter": "openai_codex_responses", "max_output_tokens": 65536},
    }}
    profiles = LLMConfig.from_dict(raw).models
    assert profiles["chat"].max_output_tokens is None
    assert profiles["codex"].max_output_tokens == 65536


def test_non_codex_null_clears_cap_with_independent_reserve():
    profile = {"model": "fixture", "adapter": "openai_chat", "max_output_tokens": None}
    settings = LLMConfig.from_dict({"model": {"summary": profile}}).models["summary"]
    assert settings.max_output_tokens is None
    assert settings.input_token_reserve == DEFAULT_MAX_OUTPUT_TOKENS


@pytest.mark.parametrize("adapter", ["openai_chat", "openai_responses", "openai_codex_responses"])
@pytest.mark.parametrize("mode,cap,source", [("omitted", None, "omitted"),
    ("null", None, "cleared"), ("explicit", 32768, "max_output_tokens")])
def test_settings_yaml_and_serialization_keep_the_same_cap_intent(adapter, mode, cap, source):
    profile = {"model": "fixture", "adapter": adapter, "context_window": 500000}
    if mode != "omitted":
        profile["max_output_tokens"] = cap
    direct = LlmModelTypeSettings(**profile)
    parsed = LLMConfig.from_dict({"model": {"test": profile}}).models["test"]
    for settings in (direct, parsed, LlmModelTypeSettings.model_validate_json(parsed.model_dump_json())):
        assert settings.max_output_tokens == cap
        assert settings.output_cap_source == source
        assert settings.input_token_reserve > 0
        assert settings.input_token_limit < settings.context_window


@pytest.mark.parametrize("cap", [True, False, 0, -1, "32768", "invalid", 1.5])
def test_invalid_caps_are_rejected_by_direct_and_yaml_settings(cap):
    profile = {"model": "fixture", "adapter": "openai_chat", "max_output_tokens": cap}
    with pytest.raises(ValueError, match="positive integer"):
        LlmModelTypeSettings(**profile)
    with pytest.raises(ValueError, match="positive integer"):
        LLMConfig.from_dict({"model": {"test": profile}})


def test_patch_missing_inherits_but_null_clears_without_resetting_explicit_reserve(tmp_path):
    catalog = LLMConfig.from_dict({"model": {"test": {"model": "fixture", "adapter": "openai_chat",
        "context_window": 500000, "max_output_tokens": 32768, "input_token_reserve": 40000}}})
    patch = tmp_path / "model.local.yaml"
    patch.write_text("model:\n  test:\n    temperature: 0.2\n")
    inherited = catalog.patch_from_yaml(patch).models["test"]
    assert inherited.max_output_tokens == 32768
    patch.write_text("model:\n  test:\n    max_output_tokens: null\n")
    cleared = catalog.patch_from_yaml(patch).models["test"]
    assert cleared.max_output_tokens is None and cleared.output_cap_source == "cleared"
    assert cleared.input_token_reserve == 40000 and cleared.input_token_limit == 460000


def test_legacy_yaml_budget_keeps_its_existing_context_and_input_meaning():
    legacy = LLMConfig.from_dict({"model": {"test": {"model": "fixture", "adapter": "openai_chat",
        "max_tokens": 512}}}).models["test"]
    assert legacy.context_window == legacy.input_token_limit == legacy.max_output_tokens == 512
    assert legacy.output_cap_source == "legacy_max_tokens"
