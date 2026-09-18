"""Model-type metadata from ModelManager to the LiteLLM retry boundary."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentloom.adapters.smolagents.models.model_manager import ModelManager
from agentloom.adapters.smolagents.models.model_types import ModelType


def _manager_and_config():
    manager = ModelManager.__new__(ModelManager)
    manager._model_cache = {}
    config = MagicMock()
    config.model_id = "test-model"
    config.adapter = "openai_chat"
    config.base_url = "http://localhost"
    config.api_key = "test-key"
    config.timeout = 30
    config.max_tokens = 1000
    config.context_window = 32000
    config.max_output_tokens = 1000
    config.temperature = 0.5
    config.requests_per_minute = 10
    config.num_retries = 3
    config.retry_delay = 1.0
    config.max_retry_delay = 60.0
    config.extra_headers = None
    config.context_cache = False
    config.system_prompt_boundary = None
    config.extra_completion_params = None
    return manager, config


def test_model_manager_sets_model_type_on_bridge_and_turn_options() -> None:
    manager, config = _manager_and_config()

    with patch.object(manager, "get_model_config", return_value=config):
        model = manager.get_smolagents_model(
            ModelType.POWERFUL,
            model_cache=False,
            logger=MagicMock(),
        )

    assert model._agent_loom_model_type == "powerful"
    assert model.options["_agent_loom_model_type"] == "powerful"


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
    assert bridge.options["_agent_loom_model_type"] == "powerful"
