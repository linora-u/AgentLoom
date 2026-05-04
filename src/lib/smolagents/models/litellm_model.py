
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

import hashlib
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
                 **kwargs):
        # Extract custom headers before passing kwargs to parent
        # This prevents the parent class from receiving unknown parameters
        extra_headers = kwargs.pop("extra_headers", None)
        self._extra_headers = extra_headers
        # self.logger stores a LoggerAdapter (internal impl detail) wrapping the AgentLogger.
        self.logger = get_logger(logger, __name__) if logger is not None else _LOG
        self.context_cache = context_cache
        self.system_prompt_boundary = system_prompt_boundary
        # Three-state tool call capability flag:
        #   "auto"  - detect at runtime from first API response
        #   "true"  - always use native tool_calls (skip detection)
        #   "false" - always use text parsing fallback (skip detection)
        self.supports_native_tool_calls = supports_native_tool_calls.lower().strip()
        # Runtime detection cache: None means not yet detected.
        # Set to True/False after first API call in auto mode.
        self._native_tool_calls_detected: Optional[bool] = None
        # Agent ID injected by upper-level Agent at runtime (used for tracing).
        self.agent_id = None
        # Cache break detection state
        self._prev_system_hash: Optional[str] = None
        self._prev_tools_hash: Optional[str] = None
        self._prev_model_id: Optional[str] = None
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

        # Only add extra_headers if they were provided during initialization
        # This avoids passing None or empty dict to the API
        if self._extra_headers:
            completion_kwargs["extra_headers"] = self._extra_headers

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

            # Detect cache breaks before injection (log-only, non-blocking)
            self._detect_cache_break(completion_kwargs)

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

    def _detect_cache_break(self, completion_kwargs: Dict[str, Any]):
        """
        Lightweight cache break detection. Computes hashes of system prompt and
        tool schemas, compares with previous request. Logs changes for diagnostics.
        Non-blocking — exceptions are caught and swallowed.
        """
        try:
            messages = completion_kwargs.get("messages", [])
            tools = completion_kwargs.get("tools", [])

            # Hash system prompt content
            system_msg = next(
                (m for m in messages if m.get("role") == "system"), None
            )
            system_hash = None
            if system_msg:
                raw = system_msg.get("content", "")
                if isinstance(raw, str):
                    system_hash = hashlib.md5(raw.encode("utf-8")).hexdigest()
                elif isinstance(raw, list):
                    text = "".join(
                        b.get("text", "") for b in raw if isinstance(b, dict)
                    )
                    system_hash = hashlib.md5(text.encode("utf-8")).hexdigest()

            # Hash tool schemas
            tools_hash = None
            if tools:
                tools_str = json.dumps(tools, sort_keys=True, default=str)
                tools_hash = hashlib.md5(tools_str.encode("utf-8")).hexdigest()

            current_model = self.model_id

            # Compare with previous state (skip logging on first request)
            if self._prev_system_hash is not None:
                if system_hash != self._prev_system_hash:
                    self.logger.info(
                        f"[CacheBreak] system_prompt changed "
                        f"(prev={self._prev_system_hash[:8]}, "
                        f"new={system_hash[:8] if system_hash else 'None'})"
                    )

            if self._prev_tools_hash is not None:
                if tools_hash != self._prev_tools_hash:
                    self.logger.info("[CacheBreak] tool_schemas changed")

            if self._prev_model_id is not None:
                if current_model != self._prev_model_id:
                    self.logger.info(
                        f"[CacheBreak] model changed "
                        f"from {self._prev_model_id} to {current_model}"
                    )

            # Store current state for next comparison
            self._prev_system_hash = system_hash
            self._prev_tools_hash = tools_hash
            self._prev_model_id = current_model

        except Exception as e:
            self.logger.debug(f"Cache break detection error (non-blocking): {e}")
