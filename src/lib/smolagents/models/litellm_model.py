
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

import uuid
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from smolagents import AgentLogger, LiteLLMModel
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole
from src.lib.logging import get_logger
from src.lib.smolagents.models.tool_call_parser import (
    ToolCallParseError,
    parse_json_with_repair,
    parse_structured_tool_call,
)

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
                 supports_structured_output: str = "false",
                 **kwargs):
        if "supports_native_tool_calls" in kwargs:
            raise TypeError(
                "supports_native_tool_calls was removed. AgentLoom now sends structured tool schemas "
                "whenever tools are available and rejects malformed tool calls explicitly."
            )

        # extra_headers flows through to self.kwargs via parent __init__,
        # then gets injected into every litellm.completion() call natively.
        # self.logger stores a LoggerAdapter (internal impl detail) wrapping the AgentLogger.
        self.logger = get_logger(logger, __name__) if logger is not None else _LOG
        self.context_cache = context_cache
        self.system_prompt_boundary = system_prompt_boundary
        # Whether the model supports json_schema structured output.
        # "true" - use structured output (json_schema) for code_act mode
        # "false" - use text-based <code> block parsing for code_act mode
        self.supports_structured_output = supports_structured_output.lower().strip()
        # Model instances are cached and shared by concurrent worker agents.
        # Invocation-specific tracing/tool schema state must therefore live in
        # ContextVars rather than mutable instance attributes.
        self._agent_id_context: ContextVar[Any | None] = ContextVar(
            f"agentloom_model_agent_id_{id(self)}",
            default=None,
        )
        self._tools_to_call_from_context: ContextVar[tuple[Any, ...]] = ContextVar(
            f"agentloom_model_tools_{id(self)}",
            default=(),
        )
        super().__init__(*args, **kwargs)

    @property
    def agent_id(self) -> Any | None:
        """Return the tracing identity bound to the current execution context."""
        context = getattr(self, "_agent_id_context", None)
        return context.get() if context is not None else None

    @agent_id.setter
    def agent_id(self, value: Any | None) -> None:
        context = getattr(self, "_agent_id_context", None)
        if context is None:
            context = ContextVar(
                f"agentloom_model_agent_id_{id(self)}",
                default=None,
            )
            self._agent_id_context = context
        context.set(value)

    def _set_current_tools(self, tools_to_call_from: list[Any] | None) -> tuple[Any, ...]:
        tools = tuple(tools_to_call_from or ())
        context = getattr(self, "_tools_to_call_from_context", None)
        if context is None:
            context = ContextVar(
                f"agentloom_model_tools_{id(self)}",
                default=(),
            )
            self._tools_to_call_from_context = context
        context.set(tools)
        return tools

    def _current_tools(self) -> list[Any]:
        context = getattr(self, "_tools_to_call_from_context", None)
        if context is not None:
            return list(context.get())
        # Compatibility for lightweight test doubles that delegate only this
        # parser method without running LiteLLMModelV2.__init__.
        return list(getattr(self, "_last_tools_to_call_from", []) or [])

    def _clear_current_tools(self) -> None:
        context = getattr(self, "_tools_to_call_from_context", None)
        if context is not None:
            context.set(())

    def generate(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list[Any] | None = None,
        **kwargs,
    ) -> ChatMessage:
        current_tools = self._set_current_tools(tools_to_call_from)
        try:
            message = super().generate(
                messages,
                stop_sequences=stop_sequences,
                response_format=response_format,
                tools_to_call_from=tools_to_call_from,
                **kwargs,
            )
            self._normalize_and_validate_tool_calls(message, list(current_tools))
            # ToolCallingAgent invokes parse_tool_calls(message) immediately
            # after generate() returns. Keep this call's schema in its Context
            # until that parser consumes it; the next generate overwrites it.
            return message
        except BaseException:
            self._clear_current_tools()
            raise

    def parse_tool_calls(self, message: ChatMessage) -> ChatMessage:
        """Parse text fallback tool calls without patching smolagents globals."""
        current_tools = LiteLLMModelV2._current_tools(self)
        try:
            message.role = MessageRole.ASSISTANT
            if not message.tool_calls:
                if message.content is None:
                    raise ToolCallParseError("Message contains no content and no tool calls")
                available_tool_names = [
                    tool.name for tool in current_tools if hasattr(tool, "name")
                ]
                candidate = parse_structured_tool_call(
                    str(message.content),
                    available_tool_names=available_tool_names or None,
                    tool_name_key=self.tool_name_key,
                    tool_arguments_key=self.tool_arguments_key,
                    model_id=self.model_id,
                )
                message.tool_calls = [
                    ChatMessageToolCall(
                        id=candidate.id or str(uuid.uuid4()),
                        type="function",
                        function=ChatMessageToolCallFunction(
                            name=candidate.name,
                            arguments=candidate.arguments,
                        ),
                    )
                ]

            if not message.tool_calls:
                raise ToolCallParseError("No tool call was found in the model output")

            LiteLLMModelV2._normalize_and_validate_tool_calls(
                message,
                current_tools,
            )
            return message
        finally:
            LiteLLMModelV2._clear_current_tools(self)

    @staticmethod
    def _normalize_tool_arguments(arguments: Any) -> Any:
        """Normalize provider tool-call argument payloads.

        Native tool_call arguments can arrive as JSON strings or, with some
        providers/proxies, as a double-encoded JSON string. Only JSON payloads
        are parsed here; non-JSON strings remain unchanged and fail later during
        normal tool validation/execution.
        """

        parsed = arguments
        if not isinstance(parsed, str):
            return parsed

        original = parsed
        for _ in range(2):
            if not isinstance(parsed, str):
                return parsed
            try:
                parsed = parse_json_with_repair(parsed)
            except Exception:
                return original
        return parsed

    @staticmethod
    def _available_tool_names(tools_to_call_from: list[Any]) -> set[str]:
        return {tool.name for tool in tools_to_call_from if hasattr(tool, "name")}

    @staticmethod
    def _normalize_and_validate_tool_calls(
        message: ChatMessage,
        tools_to_call_from: list[Any],
    ) -> None:
        if not message.tool_calls:
            return

        available_tool_names = LiteLLMModelV2._available_tool_names(tools_to_call_from)
        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            if not isinstance(tool_name, str) or not tool_name:
                raise ToolCallParseError("Malformed tool_call: missing function name")
            if available_tool_names and tool_name not in available_tool_names:
                raise ToolCallParseError(
                    f"Tool '{tool_name}' not found in registered tools {sorted(available_tool_names)}"
                )
            tool_call.function.arguments = LiteLLMModelV2._normalize_tool_arguments(tool_call.function.arguments)

    def _prepare_completion_kwargs(self, *args, **kwargs):
        completion_kwargs = super()._prepare_completion_kwargs(*args, **kwargs)

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

    def _apply_rate_limit(self) -> None:
        """AgentLoom applies model-type rate limiting in litellm_retry."""

        return None

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
