"""
Tests for model_type passthrough chain:
  model_manager → litellm_model → litellm_retry wrapper

Verifies that _agent_loom_model_type flows correctly through the call chain.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestModelTypePassthrough:
    def test_model_manager_sets_agent_loom_model_type(self):
        """get_smolagents_model should set _agent_loom_model_type on the model."""
        from src.lib.smolagents.models.model_manager import ModelManager

        manager = ModelManager.__new__(ModelManager)
        manager._model_cache = {}

        # Mock get_model_config to return a config-like object
        mock_config = MagicMock()
        mock_config.model_id = "test-model"
        mock_config.base_url = "http://localhost"
        mock_config.api_key = "test-key"
        mock_config.timeout = 30
        mock_config.max_tokens = 1000
        mock_config.temperature = 0.5
        mock_config.requests_per_minute = 10
        mock_config.num_retries = 3
        mock_config.retry_delay = 1.0
        mock_config.max_retry_delay = 60.0
        mock_config.extra_headers = None
        mock_config.context_cache = False

        mock_logger = MagicMock()
        with patch.object(manager, "get_model_config", return_value=mock_config):
            from src.lib.smolagents.models.model_types import ModelType
            model = manager.get_smolagents_model(ModelType.POWERFUL, model_cache=False, logger=mock_logger)
            assert hasattr(model, "_agent_loom_model_type")
            assert model._agent_loom_model_type == "powerful"

    def test_litellm_model_passes_model_type_in_kwargs(self):
        """_prepare_completion_kwargs should include _agent_loom_model_type."""
        from src.lib.smolagents.models.litellm_model import LiteLLMModelV2

        model = LiteLLMModelV2.__new__(LiteLLMModelV2)
        model.context_cache = False
        model._agent_loom_model_type = "fast"

        # Mock super()._prepare_completion_kwargs
        with patch("smolagents.LiteLLMModel._prepare_completion_kwargs", return_value={"model": "test"}):
            result = model._prepare_completion_kwargs()
            assert "_agent_loom_model_type" in result
            assert result["_agent_loom_model_type"] == "fast"

    def test_missing_model_type_no_error(self):
        """If _agent_loom_model_type not set, _prepare_completion_kwargs still works."""
        from src.lib.smolagents.models.litellm_model import LiteLLMModelV2

        model = LiteLLMModelV2.__new__(LiteLLMModelV2)
        model.context_cache = False
        # Deliberately NOT setting _agent_loom_model_type

        with patch("smolagents.LiteLLMModel._prepare_completion_kwargs", return_value={"model": "test"}):
            result = model._prepare_completion_kwargs()
            assert "_agent_loom_model_type" not in result
