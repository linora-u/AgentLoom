"""Model-type metadata from ModelManager to the LiteLLM retry boundary."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentloom.adapters.smolagents.models.model_manager import (
    ModelConfigBuilder,
    ModelConfigOverlay,
    ModelManager,
)
from agentloom.adapters.smolagents.models.model_types import (
    ModelConfig,
    ModelType,
    ModelTypeManager,
)


def _manager_and_config():
    manager = ModelManager.__new__(ModelManager)
    manager._model_cache = {}
    config = ModelConfig(
        model_id="test-model",
        adapter="openai_chat",
        base_url="http://localhost",
        api_key="test-key",
        timeout=30,
        max_tokens=1000,
        context_window=32000,
        max_output_tokens=1000,
        input_token_limit=31000,
        temperature=0.5,
        requests_per_minute=10,
        num_retries=3,
        retry_delay=1.0,
        max_retry_delay=60.0,
        extra_headers=None,
        context_cache=False,
        system_prompt_boundary=None,
        extra_completion_params=None,
    )
    return manager, config


def test_model_manager_sets_model_type_on_binding_and_turn_options() -> None:
    manager, config = _manager_and_config()

    with patch.object(manager, "get_model_config", return_value=config):
        model = manager.get_smolagents_model(
            ModelType.POWERFUL,
            model_cache=False,
            logger=MagicMock(),
        )

    assert model.binding.model_type == "powerful"
    assert model.binding.options["_agent_loom_model_type"] == "powerful"


def test_model_manager_litellm_config_carries_model_type_only_at_bridge_boundary() -> None:
    manager, config = _manager_and_config()

    with patch.object(manager, "get_model_config", return_value=config):
        ordinary = manager.get_litellm_config(
            ModelType.POWERFUL,
            model_cache=False,
        )
        bridge = manager.get_smolagents_model(
            ModelType.POWERFUL,
            model_cache=False,
        )

    assert "_agent_loom_model_type" not in ordinary
    assert bridge.binding.options["_agent_loom_model_type"] == "powerful"


def test_model_manager_projects_overlayed_profile_into_cached_binding() -> None:
    manager, config = _manager_and_config()
    builder = ModelConfigBuilder().apply_overlay(
        ModelConfigOverlay(
            temperature=0.9,
            context_window=40_000,
            max_output_tokens=2_000,
            num_retries=0,
            extra_headers={"X-Overlay": "yes"},
            context_cache=True,
        ),
        source="test overlay",
    )

    with patch.object(
        ModelTypeManager,
        "get_llm_config",
        return_value=config,
    ):
        first = manager.get_smolagents_model(
            ModelType.POWERFUL,
            model_builder=builder,
        )
        second = manager.get_smolagents_model(
            ModelType.POWERFUL,
            model_builder=builder,
        )

    assert first is second
    assert first.model_id == "test-model"
    assert first.binding.model_type == "powerful"
    assert first.binding.adapter_id == "openai_chat"
    assert first.binding.context_window == 40_000
    assert first.binding.max_output_tokens == 2_000
    assert first.binding.input_token_limit == 38_000
    assert first.binding.requests_per_minute == 10
    assert first.binding.options["temperature"] == 0.9
    assert first.binding.options["num_retries"] == 0
    assert first.binding.options["extra_headers"]["X-Overlay"] == "yes"
    assert first.binding.adapter._context_cache is True
