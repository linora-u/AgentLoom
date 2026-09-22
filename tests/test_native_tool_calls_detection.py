"""Strict native tool-call configuration and bridge state."""

import pytest
from agentloom.runtimes.smolagents.model_turn_bridge import SmolagentsModelTurnBridge
from agentloom.runtimes.smolagents.models.model_types import ModelConfig
from agentloom.configuration.llm_config import LLMConfig, LlmModelTypeSettings
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import ModelTurnRequest, ModelTurnResult


class _EmptyAdapter:
    adapter_id = "openai_chat"

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult()


def _binding() -> ModelTurnBinding:
    return ModelTurnBinding(
        model_type="test",
        model_id="test/model",
        adapter=_EmptyAdapter(),
    )


def test_model_config_has_no_native_tool_call_detection_field() -> None:
    assert not hasattr(
        ModelConfig(adapter="openai_chat"),
        "supports_native_tool_calls",
    )


def test_model_config_requires_explicit_adapter() -> None:
    with pytest.raises(TypeError, match="adapter"):
        ModelConfig()  # type: ignore[call-arg]


def test_bridge_has_no_native_tool_call_detection_state() -> None:
    model = SmolagentsModelTurnBridge(binding=_binding())

    assert not hasattr(model, "supports_native_tool_calls")
    assert not hasattr(model, "_native_tool_calls_detected")
    assert not hasattr(model, "should_use_native_tool_calls")
    assert not hasattr(model, "update_native_tool_calls_detection")


def test_bridge_rejects_removed_supports_native_tool_calls_constructor_arg() -> None:
    with pytest.raises(TypeError):
        SmolagentsModelTurnBridge(
            binding=_binding(),
            supports_native_tool_calls=False,
        )


def test_llm_model_type_settings_has_no_native_tool_call_detection_field() -> None:
    settings = LlmModelTypeSettings(model="test/model", adapter="openai_chat")
    assert not hasattr(settings, "supports_native_tool_calls")


def test_llm_model_type_settings_rejects_removed_native_tool_call_detection_field() -> None:
    with pytest.raises(ValueError, match="supports_native_tool_calls"):
        LlmModelTypeSettings(
            model="test/model",
            adapter="openai_chat",
            supports_native_tool_calls="false",
        )


def test_llm_config_rejects_removed_supports_native_tool_calls_field() -> None:
    with pytest.raises(ValueError, match="supports_native_tool_calls"):
        LLMConfig.from_dict(
            {
                "model": {
                    "powerful": {
                        "model": "test/powerful",
                        "adapter": "openai_chat",
                        "supports_native_tool_calls": "false",
                    },
                    "summary": {
                        "model": "test/summary",
                        "adapter": "openai_chat",
                    },
                }
            }
        )


def test_llm_config_keeps_tool_choice_as_extra_completion_param() -> None:
    config = LLMConfig.from_dict(
        {
            "model": {
                "powerful": {
                    "model": "test/powerful",
                    "adapter": "openai_chat",
                    "tool_choice": "auto",
                },
                "summary": {
                    "model": "test/summary",
                    "adapter": "openai_chat",
                },
            }
        }
    )

    assert config.models["powerful"].extra_completion_params == {
        "tool_choice": "auto"
    }
