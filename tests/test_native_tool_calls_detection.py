"""
Tests for the supports_native_tool_calls configuration and detection logic.

Covers:
- LiteLLMModelV2 three-state flag: auto, true, false
- Runtime detection cache update
- should_use_native_tool_calls decision logic
- ModelConfig field propagation
- LlmModelTypeSettings field parsing
"""

import pytest
from unittest.mock import MagicMock, patch

from src.lib.smolagents.models.model_types import ModelConfig


class TestModelConfigField:
    """Test supports_native_tool_calls field in ModelConfig."""

    def test_default_is_auto(self):
        config = ModelConfig()
        assert config.supports_native_tool_calls == "auto"

    def test_explicit_true(self):
        config = ModelConfig(supports_native_tool_calls="true")
        assert config.supports_native_tool_calls == "true"

    def test_explicit_false(self):
        config = ModelConfig(supports_native_tool_calls="false")
        assert config.supports_native_tool_calls == "false"


class TestLiteLLMModelV2Detection:
    """Test detection logic in LiteLLMModelV2."""

    @pytest.fixture
    def make_model(self):
        """Create a mock LiteLLMModelV2 with detection attributes."""
        def _make(supports="auto"):
            model = MagicMock()
            model.supports_native_tool_calls = supports
            model._native_tool_calls_detected = None
            model.model_id = "test-model"
            model.logger = MagicMock()

            # Import the actual methods and bind them
            from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
            model.should_use_native_tool_calls = LiteLLMModelV2.should_use_native_tool_calls.__get__(model)
            model.update_native_tool_calls_detection = LiteLLMModelV2.update_native_tool_calls_detection.__get__(model)
            return model
        return _make

    def test_true_always_returns_true(self, make_model):
        model = make_model("true")
        assert model.should_use_native_tool_calls() is True

    def test_false_always_returns_false(self, make_model):
        model = make_model("false")
        assert model.should_use_native_tool_calls() is False

    def test_auto_first_call_returns_true(self, make_model):
        """Auto mode: first call should return True (try native)."""
        model = make_model("auto")
        assert model.should_use_native_tool_calls() is True

    def test_auto_after_detection_true(self, make_model):
        """Auto mode: after detecting native tool_calls, should return True."""
        model = make_model("auto")
        model.update_native_tool_calls_detection(True)
        assert model._native_tool_calls_detected is True
        assert model.should_use_native_tool_calls() is True

    def test_auto_after_detection_false(self, make_model):
        """Auto mode: after detecting no native tool_calls, should return False."""
        model = make_model("auto")
        model.update_native_tool_calls_detection(False)
        assert model._native_tool_calls_detected is False
        assert model.should_use_native_tool_calls() is False

    def test_detection_only_updates_once(self, make_model):
        """Detection cache should only update once (first observation wins)."""
        model = make_model("auto")
        model.update_native_tool_calls_detection(False)
        assert model._native_tool_calls_detected is False

        # Second update should be ignored
        model.update_native_tool_calls_detection(True)
        assert model._native_tool_calls_detected is False

    def test_detection_skipped_for_true(self, make_model):
        """Detection should not update when mode is 'true'."""
        model = make_model("true")
        model.update_native_tool_calls_detection(False)
        assert model._native_tool_calls_detected is None

    def test_detection_skipped_for_false(self, make_model):
        """Detection should not update when mode is 'false'."""
        model = make_model("false")
        model.update_native_tool_calls_detection(True)
        assert model._native_tool_calls_detected is None


class TestLlmConfigParsing:
    """Test supports_native_tool_calls in YAML config parsing."""

    def test_default_auto_in_yaml(self):
        """When not specified in YAML, should default to 'auto'."""
        from src.lib.config.llm_config import LlmModelTypeSettings
        settings = LlmModelTypeSettings(model="test/model")
        assert settings.supports_native_tool_calls == "auto"

    def test_explicit_value_in_yaml(self):
        """Explicit values should be preserved."""
        from src.lib.config.llm_config import LlmModelTypeSettings
        settings = LlmModelTypeSettings(model="test/model", supports_native_tool_calls="false")
        assert settings.supports_native_tool_calls == "false"

    def test_from_dict_with_field(self):
        """LLMConfig.from_dict should parse supports_native_tool_calls."""
        from src.lib.config.llm_config import LLMConfig

        raw = {
            "model": {
                "common": {
                    "model": "test/common",
                },
                "powerful": {
                    "model": "test/powerful",
                    "supports_native_tool_calls": "false",
                },
                "summary": {
                    "model": "test/summary",
                },
            },
        }
        config = LLMConfig.from_dict(raw)
        assert config.models["powerful"].supports_native_tool_calls == "false"
        # summary should default to common's value (auto by default)
        assert config.models["summary"].supports_native_tool_calls == "auto"

    def test_from_dict_invalid_value_defaults_to_auto(self):
        """Invalid values should default to 'auto'."""
        from src.lib.config.llm_config import LLMConfig

        raw = {
            "model": {
                "common": {
                    "model": "test/common",
                },
                "powerful": {
                    "model": "test/powerful",
                    "supports_native_tool_calls": "invalid_value",
                },
                "summary": {
                    "model": "test/summary",
                },
            },
        }
        config = LLMConfig.from_dict(raw)
        assert config.models["powerful"].supports_native_tool_calls == "auto"

    def test_common_value_inherited(self):
        """Common-level supports_native_tool_calls should be inherited."""
        from src.lib.config.llm_config import LLMConfig

        raw = {
            "model": {
                "common": {
                    "model": "test/common",
                    "supports_native_tool_calls": "false",
                },
                "powerful": {
                    "model": "test/powerful",
                },
                "summary": {
                    "model": "test/summary",
                },
            },
        }
        config = LLMConfig.from_dict(raw)
        # Should inherit from common
        assert config.models["powerful"].supports_native_tool_calls == "false"

    def test_type_level_overrides_common(self):
        """Type-level value should override common value."""
        from src.lib.config.llm_config import LLMConfig

        raw = {
            "model": {
                "common": {
                    "model": "test/common",
                    "supports_native_tool_calls": "false",
                },
                "powerful": {
                    "model": "test/powerful",
                    "supports_native_tool_calls": "true",
                },
                "summary": {
                    "model": "test/summary",
                },
            },
        }
        config = LLMConfig.from_dict(raw)
        assert config.models["powerful"].supports_native_tool_calls == "true"
        assert config.models["summary"].supports_native_tool_calls == "false"
