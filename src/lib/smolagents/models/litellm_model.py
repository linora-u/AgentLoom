
"""
Extended LiteLLM model implementation with custom header support.

This module provides an enhanced version of the smolagents LiteLLMModel that allows
passing custom HTTP headers to the underlying LLM API calls. This is particularly
useful for authentication, request tracking, or other API-specific requirements.

Automatic prompt caching:
  When context_cache=True, cache_control: {"type": "ephemeral"} is injected into
  system message content blocks for ALL models universally. litellm handles
  per-provider transformation internally — Anthropic/Bedrock preserves it,
  OpenAI/Azure/Fireworks strips it, Vertex AI converts it to Gemini format.
  No provider-specific branching is needed in this code.
"""

import json
from typing import List, Dict, Any, Optional
from smolagents import AgentLogger, LiteLLMModel
from src.lib.logging import get_logger

_LOG = get_logger(__name__)


class LiteLLMModelV2(LiteLLMModel):
    """
    Enhanced LiteLLM model with support for custom HTTP headers and
    universal prompt caching.

    This class extends the base LiteLLMModel to allow passing custom headers
    to the underlying LLM API requests. This is useful for scenarios where
    you need to:
    - Add custom authentication headers
    - Include request tracking headers
    - Pass API-specific metadata
    - Implement custom rate limiting or monitoring

    The custom headers are preserved and passed through to all completion
    requests made by this model instance.
    """
    def __init__(self, *args, logger: Optional[AgentLogger] = None,
                 context_cache=False, system_prompt_boundary: Optional[str] = None,
                 supports_native_tool_calls: str = "auto",
                 supports_structured_output: str = "false",
                 **kwargs):
        # extra_headers flows through to self.kwargs via parent __init__,
        # then gets injected into every litellm.completion() call natively.
        # self.logger stores a LoggerAdapter (internal impl detail) wrapping the AgentLogger.
        self.logger = get_logger(logger, __name__) if logger is not None else _LOG
        self.context_cache = context_cache
        self.system_prompt_boundary = system_prompt_boundary
        # Three-state tool call capability flag:
        #   "auto"  - detect at runtime from first API response
        #   "true"  - always use native tool_calls (skip detection)
        #   "false" - always use text parsing fallback (skip detection)
        self.supports_native_tool_calls = supports_native_tool_calls.lower().strip()
        # Whether the model supports json_schema structured output.
        # "true" - use structured output (json_schema) for code_act mode
        # "false" - use text-based <code> block parsing for code_act mode
        self.supports_structured_output = supports_structured_output.lower().strip()
        # Runtime detection cache: None means not yet detected.
        # Set to True/False after first API call in auto mode.
        self._native_tool_calls_detected: Optional[bool] = None
        # Agent ID injected by upper-level Agent at runtime (used for tracing).
        self.agent_id = None
        super().__init__(*args, **kwargs)

    def should_use_native_tool_calls(self) -> bool:
        """Determine whether native tool_calls path should be used.

        Returns True if:
        - supports_native_tool_calls is "true", OR
        - supports_native_tool_calls is "auto" and detection result is True

        Returns False if:
        - supports_native_tool_calls is "false", OR
        - supports_native_tool_calls is "auto" and detection result is False
        - supports_native_tool_calls is "auto" and not yet detected (first call)
        """
        if self.supports_native_tool_calls == "true":
            return True
        if self.supports_native_tool_calls == "false":
            return False
        # auto mode: use cached detection result
        if self._native_tool_calls_detected is not None:
            return self._native_tool_calls_detected
        # Not yet detected: for the first call, try native (send tool schemas).
        # Detection happens after seeing the response.
        return True

    def update_native_tool_calls_detection(self, has_tool_calls: bool) -> None:
        """Update the runtime detection cache after observing an API response.

        Only updates when in "auto" mode and not yet detected.

        Args:
            has_tool_calls: Whether the API response contained tool_calls.
        """
        if self.supports_native_tool_calls != "auto":
            return
        if self._native_tool_calls_detected is not None:
            return
        self._native_tool_calls_detected = has_tool_calls
        self.logger.info(
            "Native tool_calls detection: model=%s, detected=%s",
            self.model_id,
            has_tool_calls,
        )

    def _prepare_completion_kwargs(self, *args, **kwargs):
        completion_kwargs = super()._prepare_completion_kwargs(*args, **kwargs)

        if getattr(self, "supports_native_tool_calls", "auto") == "false":
            removed_tool_choice = completion_kwargs.pop("tool_choice", None)
            self.logger.debug(
                "[DEBUG_TOOL_CHOICE] supports_native_tool_calls=false; "
                "removed tool_choice=%r; has_tools=%s; extra_body=%s",
                removed_tool_choice,
                "tools" in completion_kwargs,
                completion_kwargs.get("extra_body"),
            )

        # Remove </code> from stop sequences. smolagents adds it as a stop token,
        # but some APIs (e.g. ARK) return partial residue ("</cod") when given
        # multiple stops. Removing it is safe: parse_code_blobs uses regex to
        # extract code blocks regardless of whether the response was truncated.
        if "stop" in completion_kwargs and completion_kwargs["stop"]:
            completion_kwargs["stop"] = [s for s in completion_kwargs["stop"] if s != "</code>"]

        # Inject model_type for global rate limiting in litellm_retry wrapper
        if hasattr(self, "_agent_loom_model_type"):
            completion_kwargs["_agent_loom_model_type"] = self._agent_loom_model_type

        # Apply Automatic Caching Logic
        self._apply_automatic_caching(completion_kwargs)

        return completion_kwargs

    def _apply_automatic_caching(self, completion_kwargs: Dict[str, Any]):
        """
        Apply universal prompt caching by injecting cache_control into system
        message content blocks. Works for ALL models — litellm handles
        per-provider transformation (preserve, strip, or convert cache_control).
        """
        try:
            if not self.context_cache:
                return

            messages = completion_kwargs.get("messages", [])

            # Inject cache_control into system message — universal for all providers
            self._inject_cache_control(messages)

        except Exception as e:
            self.logger.warning(f"Failed to apply automatic caching: {e}")

    def _inject_cache_control(self, messages: List[Dict[str, Any]]):
        """
        Inject cache_control: {"type": "ephemeral"} into system message content blocks.

        litellm handles per-provider behavior automatically:
        - Anthropic/Bedrock: preserves cache_control (native support)
        - Vertex AI: converts to Gemini context caching format
        - OpenAI/Azure: strips cache_control (their caching is automatic by prefix)
        - OpenRouter: preserves for Claude/Gemini, strips for others
        - Fireworks/others: strips cache_control via default transformation
        """
        system_msg = next((m for m in messages if m.get("role") == "system"), None)
        if not system_msg:
            return

        content = system_msg.get("content")
        if not content:
            return

        boundary = self.system_prompt_boundary

        # Handle string content
        if isinstance(content, str):
            if boundary and boundary in content:
                # Split into static (cached) + dynamic (uncached) blocks
                static_part, dynamic_part = content.split(boundary, 1)
                blocks = []
                if static_part.strip():
                    blocks.append({
                        "type": "text",
                        "text": static_part,
                        "cache_control": {"type": "ephemeral"}
                    })
                if dynamic_part.strip():
                    blocks.append({
                        "type": "text",
                        "text": dynamic_part
                    })
                if blocks:
                    system_msg["content"] = blocks
                    self.logger.info(
                        f"[AutoCache] Split system prompt into {len(blocks)} blocks "
                        f"(static cached + dynamic) for {self.model_id}."
                    )
            else:
                # No boundary — cache the entire content as one block
                system_msg["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"}
                    }
                ]
                self.logger.info(
                    f"[AutoCache] Injected cache_control into system prompt "
                    f"for {self.model_id}."
                )

        # Handle list content (already in block format)
        elif isinstance(content, list) and len(content) > 0:
            last_block = content[-1]
            if isinstance(last_block, dict) and "cache_control" not in last_block:
                last_block["cache_control"] = {"type": "ephemeral"}


