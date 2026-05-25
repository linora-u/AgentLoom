"""
Tests for universal context cache injection in LiteLLMModelV2.

Covers:
- Universal cache_control injection for all model providers
- System prompt boundary splitting
- Cache break detection
- Disabled caching
- Error handling
- Regression: no "unsupported model" warning
"""

import json
from unittest.mock import patch, MagicMock
import pytest

from src.lib.smolagents.models.litellm_model import LiteLLMModelV2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model(model_id="anthropic/claude-sonnet-4-20250514",
                context_cache=True,
                system_prompt_boundary=None):
    """Create a LiteLLMModelV2 instance with mocked parent init."""
    with patch.object(LiteLLMModelV2, '__init__', lambda self, *a, **kw: None):
        m = LiteLLMModelV2.__new__(LiteLLMModelV2)
        m.logger = MagicMock()
        m.context_cache = context_cache
        m.system_prompt_boundary = system_prompt_boundary
        m.agent_id = None
        m.model_id = model_id
    return m


def _completion_kwargs(system_content="You are helpful.", tools=None):
    """Build a minimal completion_kwargs dict."""
    kwargs = {
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "Hello"},
        ],
    }
    if tools is not None:
        kwargs["tools"] = tools
    return kwargs


# ===========================================================================
# 5.1 — TestUniversalCacheInjection
# ===========================================================================

class TestUniversalCacheInjection:
    """Universal cache_control injection works identically for all model providers."""

    @pytest.mark.parametrize("model_id", [
        "anthropic/claude-sonnet-4-20250514",
        "anthropic/MiniMax-M2.7",
        "openai/gpt-4o",
        "gemini/gemini-2.0-flash",
        "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "vertex_ai/gemini-pro",
        "cohere/command-r-plus",
        "fireworks_ai/llama-v3p1-8b-instruct",
    ])
    def test_all_models_get_same_injection(self, model_id):
        """Every model gets cache_control injected on system message."""
        model = _make_model(model_id=model_id)
        kwargs = _completion_kwargs("You are a coding assistant.")
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list), f"Expected list for {model_id}"
        assert len(content) == 1
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[0]["text"] == "You are a coding assistant."

    def test_string_content_converted_to_block_list(self):
        """String content is converted to a list with one cached block."""
        model = _make_model()
        kwargs = _completion_kwargs("Hello world")
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert len(content) == 1
        block = content[0]
        assert block["type"] == "text"
        assert block["text"] == "Hello world"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_list_content_last_block_gets_cache_control(self):
        """When content is already a list, cache_control goes on the last block."""
        model = _make_model()
        blocks = [
            {"type": "text", "text": "Part 1"},
            {"type": "text", "text": "Part 2"},
        ]
        kwargs = _completion_kwargs(blocks)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        # First block unchanged
        assert "cache_control" not in content[0]
        # Last block gets cache_control
        assert content[1]["cache_control"] == {"type": "ephemeral"}

    def test_duplicate_cache_control_prevented(self):
        """If last block already has cache_control, don't add another."""
        model = _make_model()
        blocks = [
            {"type": "text", "text": "Already cached",
             "cache_control": {"type": "ephemeral"}},
        ]
        kwargs = _completion_kwargs(blocks)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        # Verify it wasn't doubled or changed
        assert len(content) == 1

    def test_empty_list_content_no_error(self):
        """Empty list content is a no-op."""
        model = _make_model()
        kwargs = _completion_kwargs([])
        model._apply_automatic_caching(kwargs)
        assert kwargs["messages"][0]["content"] == []

    def test_none_content_no_error(self):
        """None content is a no-op."""
        model = _make_model()
        kwargs = {"messages": [{"role": "system", "content": None}]}
        model._apply_automatic_caching(kwargs)
        assert kwargs["messages"][0]["content"] is None


# ===========================================================================
# 5.2 — TestSystemPromptSplitting
# ===========================================================================

class TestSystemPromptSplitting:
    """System prompt boundary splitting into static (cached) + dynamic (uncached)."""

    def test_boundary_splits_into_two_blocks(self):
        """Content with boundary marker splits into cached static + uncached dynamic."""
        model = _make_model(system_prompt_boundary="<!-- DYNAMIC_BOUNDARY -->")
        prompt = "Static instructions<!-- DYNAMIC_BOUNDARY -->Dynamic task context"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        # Static block is cached
        assert content[0]["text"] == "Static instructions"
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        # Dynamic block is NOT cached
        assert content[1]["text"] == "Dynamic task context"
        assert "cache_control" not in content[1]

    def test_no_boundary_treats_all_as_static(self):
        """Without boundary marker, entire content is cached (default behavior)."""
        model = _make_model(system_prompt_boundary="<!-- DYNAMIC_BOUNDARY -->")
        prompt = "Full static prompt without any boundary"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert len(content) == 1
        assert content[0]["text"] == prompt
        assert content[0]["cache_control"] == {"type": "ephemeral"}

    def test_boundary_not_configured_treats_all_as_static(self):
        """When system_prompt_boundary is None, all content is cached."""
        model = _make_model(system_prompt_boundary=None)
        prompt = "Some prompt<!-- DYNAMIC_BOUNDARY -->with marker in text"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert len(content) == 1
        # The full text including marker is cached since boundary is not configured
        assert content[0]["text"] == prompt
        assert content[0]["cache_control"] == {"type": "ephemeral"}

    def test_boundary_at_start_no_static_block(self):
        """Boundary at start means empty static part — only dynamic block created."""
        model = _make_model(system_prompt_boundary="---SPLIT---")
        prompt = "---SPLIT---All dynamic content here"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert len(content) == 1
        assert content[0]["text"] == "All dynamic content here"
        assert "cache_control" not in content[0]

    def test_boundary_at_end_only_static_block(self):
        """Boundary at end means empty dynamic part — only static block created."""
        model = _make_model(system_prompt_boundary="---SPLIT---")
        prompt = "All static content here---SPLIT---"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert len(content) == 1
        assert content[0]["text"] == "All static content here"
        assert content[0]["cache_control"] == {"type": "ephemeral"}

    def test_custom_boundary_string(self):
        """Custom boundary string works correctly."""
        model = _make_model(system_prompt_boundary="### DYNAMIC ###")
        prompt = "Rules and instructions### DYNAMIC ###Current task: write tests"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert len(content) == 2
        assert content[0]["text"] == "Rules and instructions"
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[1]["text"] == "Current task: write tests"
        assert "cache_control" not in content[1]

    def test_multiple_boundaries_only_first_used(self):
        """Only the first occurrence of boundary is used for splitting."""
        model = _make_model(system_prompt_boundary="---")
        prompt = "Part A---Part B---Part C"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert len(content) == 2
        assert content[0]["text"] == "Part A"
        assert content[1]["text"] == "Part B---Part C"


# ===========================================================================
# 5.3 — TestContextCacheDisabled
# ===========================================================================

class TestContextCacheDisabled:
    """context_cache=False skips all caching logic."""

    def test_disabled_skips_caching(self):
        """No cache_control injected when context_cache=False."""
        model = _make_model(context_cache=False)
        kwargs = _completion_kwargs("System prompt")
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert isinstance(content, str)
        assert content == "System prompt"

    def test_disabled_skips_splitting(self):
        """No system prompt splitting when context_cache=False."""
        model = _make_model(context_cache=False,
                            system_prompt_boundary="<!-- DYNAMIC_BOUNDARY -->")
        prompt = "Static<!-- DYNAMIC_BOUNDARY -->Dynamic"
        kwargs = _completion_kwargs(prompt)
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert isinstance(content, str)
        assert content == prompt


# ===========================================================================
# 5.5 — TestErrorHandling
# ===========================================================================

class TestErrorHandling:
    """Error handling in caching logic."""

    def test_no_system_message_no_error(self):
        """Messages without system role are a no-op."""
        model = _make_model()
        kwargs = {"messages": [{"role": "user", "content": "Hello"}]}
        model._apply_automatic_caching(kwargs)
        # No error raised, user message untouched
        assert kwargs["messages"][0]["content"] == "Hello"

    def test_empty_messages_no_error(self):
        """Empty messages list is a no-op."""
        model = _make_model()
        kwargs = {"messages": []}
        model._apply_automatic_caching(kwargs)
        assert kwargs["messages"] == []

    def test_malformed_system_content_caught(self):
        """Unexpected content types don't crash."""
        model = _make_model()
        kwargs = {"messages": [{"role": "system", "content": 12345}]}
        model._apply_automatic_caching(kwargs)
        # Content stays as-is (not str, not list)
        assert kwargs["messages"][0]["content"] == 12345

    def test_exception_in_inject_caught(self):
        """Exception inside _inject_cache_control is caught by outer try/except."""
        model = _make_model()
        kwargs = _completion_kwargs("Normal prompt")
        # Force an exception in _inject_cache_control
        with patch.object(model, '_inject_cache_control', side_effect=RuntimeError("boom")):
            model._apply_automatic_caching(kwargs)
        # Should have logged warning, not crashed
        model.logger.warning.assert_called()


# ===========================================================================
# 5.6 — TestNoWarning (Regression)
# ===========================================================================

class TestNoWarning:
    """Regression: no 'unsupported model' warning for any model."""

    @pytest.mark.parametrize("model_id", [
        "anthropic/MiniMax-M2.7",
        "cohere/command-r-plus",
        "fireworks_ai/llama-v3p1-8b-instruct",
        "together/meta-llama-3.1-405b",
        "deepseek/deepseek-coder",
        "ollama/llama3",
    ])
    def test_no_unsupported_warning(self, model_id):
        """No model triggers an 'unsupported' or 'not support' warning."""
        model = _make_model(model_id=model_id)
        kwargs = _completion_kwargs("System prompt")
        model._apply_automatic_caching(kwargs)

        # Check no warning was logged with "unsupported" or "not support"
        for call in model.logger.warning.call_args_list:
            msg = str(call)
            assert "not support" not in msg.lower()
            assert "unsupported" not in msg.lower()

    def test_minimax_gets_cache_control(self):
        """The exact model that triggered the original bug now gets cached."""
        model = _make_model(model_id="anthropic/MiniMax-M2.7")
        kwargs = _completion_kwargs("You are an AI assistant.")
        model._apply_automatic_caching(kwargs)

        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        # No warning logged
        model.logger.warning.assert_not_called()
