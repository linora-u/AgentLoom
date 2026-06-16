"""Tests for strict native tool-call transport behavior."""

import pytest

from src.lib.config.llm_config import LLMConfig, LlmModelTypeSettings
from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
from src.lib.smolagents.models.model_types import ModelConfig


def test_model_config_has_no_native_tool_call_detection_field():
    config = ModelConfig()

    assert not hasattr(config, "supports_native_tool_calls")


def test_litellm_model_has_no_native_tool_call_detection_state():
    model = LiteLLMModelV2(model_id="test/model")

    assert not hasattr(model, "supports_native_tool_calls")
    assert not hasattr(model, "_native_tool_calls_detected")
    assert not hasattr(model, "should_use_native_tool_calls")
    assert not hasattr(model, "update_native_tool_calls_detection")


def test_litellm_model_rejects_removed_supports_native_tool_calls_constructor_arg():
    with pytest.raises(TypeError):
        LiteLLMModelV2(model_id="test/model", supports_native_tool_calls="false")


def test_llm_model_type_settings_has_no_native_tool_call_detection_field():
    settings = LlmModelTypeSettings(model="test/model")

    assert not hasattr(settings, "supports_native_tool_calls")


def test_llm_model_type_settings_rejects_removed_native_tool_call_detection_field():
    with pytest.raises(ValueError, match="supports_native_tool_calls"):
        LlmModelTypeSettings(model="test/model", supports_native_tool_calls="false")


def test_llm_config_rejects_removed_supports_native_tool_calls_field():
    raw = {
        "model": {
            "powerful": {
                "model": "test/powerful",
                "supports_native_tool_calls": "false",
            },
            "summary": {
                "model": "test/summary",
            },
        },
    }

    with pytest.raises(ValueError, match="supports_native_tool_calls"):
        LLMConfig.from_dict(raw)


def test_llm_config_keeps_tool_choice_as_extra_completion_param():
    raw = {
        "model": {
            "powerful": {
                "model": "test/powerful",
                "tool_choice": "auto",
            },
            "summary": {
                "model": "test/summary",
            },
        },
    }

    config = LLMConfig.from_dict(raw)

    assert config.models["powerful"].extra_completion_params == {"tool_choice": "auto"}
