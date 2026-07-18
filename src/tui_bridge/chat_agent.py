"""Independent short-session chat agent for the AgentLoom TUI.

This module deliberately uses the OpenAI-compatible SDK directly.  It does
not import the AgentLoom execution Agent, smolagents, or LiteLLM.  The TUI
agent owns a small conversation/tool loop and can call only the draft tools
provided by :mod:`src.tui_bridge.builder`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from src.lib.config.llm_config import LLMConfig

_MAX_PROVIDER_TURNS = 6
_MAX_PROVIDER_ATTEMPTS = 2
_MAX_OUTPUT_TOKENS = 4096
_REQUEST_TIMEOUT_SECONDS = 60.0
_WHOLE_TURN_TIMEOUT_SECONDS = 90.0
_RETRY_DELAY_SECONDS = 1.0


class _ChatTool(Protocol):
    name: str
    description: str
    inputs: Mapping[str, Mapping[str, object]]

    def forward(self, **kwargs: object) -> str: ...


@dataclass(frozen=True, slots=True)
class ChatModelProfile:
    """One safe, bounded TUI projection of an ``llm.yaml`` model entry."""

    model_type: str
    model_id: str
    base_url: str | None
    api_key: str = field(repr=False)
    request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS
    max_output_tokens: int = _MAX_OUTPUT_TOKENS
    temperature: float | None = None
    extra_headers: Mapping[str, str] | None = None
    extra_body: Mapping[str, object] | None = None
    reasoning_effort: str | None = None
    parallel_tool_calls: bool | None = None


@dataclass(frozen=True, slots=True)
class ChatAgentResult:
    assistant: str
    model_type: str


@dataclass(frozen=True, slots=True)
class _ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class _ProviderTurn:
    content: str
    tool_calls: tuple[_ToolCall, ...]


class ChatAgentError(RuntimeError):
    """A classified error whose message is safe to cross the NDJSON bridge."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


ChatClientFactory = Callable[[ChatModelProfile], Any]
EventSink = Callable[[dict[str, object]], None]


def _load_profile(project_root: Path, requested_model_type: str | None) -> ChatModelProfile:
    try:
        catalog = LLMConfig.load_from_yaml(project_root / "config" / "llm.yaml")
        settings = catalog.for_type(requested_model_type)
    except Exception as error:
        raise ChatAgentError(
            "assistant_config",
            "无法读取所选模型配置；请检查项目 config/llm.yaml。",
        ) from error

    model_type = (requested_model_type or catalog.default_model_type).strip().lower()
    raw_model_id = settings.model.strip()
    if not raw_model_id:
        raise ChatAgentError(
            "assistant_config",
            f"模型类型 {model_type!r} 没有配置 model；请检查 config/llm.yaml。",
        )
    # ``openai/`` is a LiteLLM routing prefix, not part of the provider's
    # actual model/deployment id.
    model_id = raw_model_id.removeprefix("openai/")

    base_url = settings.base_url.strip()
    if base_url:
        parsed_base_url = urlsplit(base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise ChatAgentError(
                "assistant_config",
                f"模型类型 {model_type!r} 的 base_url 无效；请检查 config/llm.yaml。",
            )

    api_key = settings.api_key.strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ChatAgentError(
            "assistant_config",
            f"模型类型 {model_type!r} 没有可用凭据；请检查 config/llm.yaml。",
        )

    raw_max_tokens = settings.max_tokens
    configured_max = raw_max_tokens if isinstance(raw_max_tokens, int) else _MAX_OUTPUT_TOKENS
    max_output_tokens = max(1, min(configured_max, _MAX_OUTPUT_TOKENS))

    extras = settings.extra_completion_params or {}
    raw_extra_body = extras.get("extra_body")
    extra_body = dict(raw_extra_body) if isinstance(raw_extra_body, Mapping) else None
    raw_reasoning_effort = extras.get("reasoning_effort")
    reasoning_effort = raw_reasoning_effort if isinstance(raw_reasoning_effort, str) else None
    raw_parallel_tool_calls = extras.get("parallel_tool_calls")
    parallel_tool_calls = raw_parallel_tool_calls if isinstance(raw_parallel_tool_calls, bool) else None
    extra_headers = None
    if settings.extra_headers:
        extra_headers = {str(key): str(value) for key, value in settings.extra_headers.items()}

    return ChatModelProfile(
        model_type=model_type,
        model_id=model_id,
        base_url=base_url or None,
        api_key=api_key,
        request_timeout_seconds=min(max(float(settings.timeout), 1.0), _REQUEST_TIMEOUT_SECONDS),
        max_output_tokens=max_output_tokens,
        temperature=settings.temperature,
        extra_headers=extra_headers,
        extra_body=extra_body,
        reasoning_effort=reasoning_effort,
        parallel_tool_calls=parallel_tool_calls,
    )


def _default_client_factory(profile: ChatModelProfile) -> OpenAI:
    return OpenAI(
        api_key=profile.api_key,
        base_url=profile.base_url,
        timeout=profile.request_timeout_seconds,
        max_retries=0,
        default_headers=profile.extra_headers,
    )


def _classify_provider_error(error: BaseException) -> ChatAgentError:
    if isinstance(error, APITimeoutError):
        return ChatAgentError(
            "assistant_timeout",
            "模型请求超时；请重试，或切换到响应更快的已配置模型。",
            retryable=True,
        )
    if isinstance(error, RateLimitError):
        return ChatAgentError(
            "assistant_rate_limit",
            "模型服务当前限流；请稍后重试，或切换其他已配置模型。",
            retryable=True,
        )
    if isinstance(error, (AuthenticationError, PermissionDeniedError)):
        return ChatAgentError(
            "assistant_auth",
            "模型认证失败；请检查 config/llm.yaml 中所选模型的凭据。",
        )
    if isinstance(error, BadRequestError):
        return ChatAgentError(
            "assistant_bad_request",
            "模型服务拒绝了请求；请检查所选模型的 model、base_url 与工具调用能力配置。",
        )
    if isinstance(error, (InternalServerError, APIConnectionError)):
        return ChatAgentError(
            "assistant_unavailable",
            "暂时无法连接模型服务；请重试，或切换其他已配置模型。",
            retryable=True,
        )
    if isinstance(error, APIStatusError):
        retryable = error.status_code in {408, 429} or error.status_code >= 500
        return ChatAgentError(
            "assistant_unavailable" if retryable else "assistant_provider_error",
            (
                "模型服务暂时不可用；请稍后重试。"
                if retryable
                else "模型服务拒绝了请求；请检查所选模型配置。"
            ),
            retryable=retryable,
        )
    if isinstance(error, ChatAgentError):
        return error
    return ChatAgentError(
        "assistant_failed",
        "模型对话失败；请重试，或切换其他已配置模型。",
    )


def _tool_schema(tool: _ChatTool) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for name, raw_spec in tool.inputs.items():
        spec = dict(raw_spec)
        properties[name] = spec
        if not bool(spec.pop("nullable", False)):
            required.append(name)
    parameters: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def _system_prompt(profile: ChatModelProfile) -> str:
    return (
        "You are the independent AgentLoom TUI assistant. Your scope is short conversation, "
        "project observation, and proposing Agent YAML.\n"
        "Answer ordinary questions directly. Do not turn a normal question into an Agent task.\n"
        "Use tools only when the user asks you to inspect an Agent System or create/change Agent YAML.\n"
        "A standalone Agent YAML must include non-empty top-level name, description, workflow, "
        "model_type, and tool_call_type fields. Put the behavioral instructions in workflow; do not "
        "rename that required field to prompt, instructions, or system_prompt.\n"
        "The only available mutations stage YAML in memory. Never claim a draft was written to disk; "
        "only the user's explicit /apply action can save it.\n"
        "Never run shell commands, execute an Agent, edit arbitrary files, or start a long task.\n"
        "Ask one concise clarification if an Agent request is materially ambiguous.\n"
        f"The selected model profile is {profile.model_type!r}; its provider model/deployment id is "
        f"{profile.model_id!r}. If asked which model you are, report these configured identifiers "
        "and do not invent a vendor or model family."
    )


class TuiChatAgent:
    """A small OpenCode/Hermes-style provider + tool loop for the TUI."""

    def __init__(
        self,
        project_root: Path | str,
        *,
        client_factory: ChatClientFactory | None = None,
        retry_sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._project_root = Path(project_root).expanduser().resolve()
        self._client_factory = client_factory or _default_client_factory
        self._retry_sleep = retry_sleep
        self._monotonic = monotonic

    def run(
        self,
        *,
        history: Sequence[Mapping[str, str]],
        model_type: str | None,
        tools: Sequence[_ChatTool],
        on_event: EventSink | None = None,
    ) -> ChatAgentResult:
        profile = _load_profile(self._project_root, model_type)
        client = self._client_factory(profile)
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _system_prompt(profile)},
            *[dict(message) for message in history],
        ]
        tool_by_name = {tool.name: tool for tool in tools}
        schemas = [_tool_schema(tool) for tool in tools]
        visible_parts: list[str] = []
        deadline = self._monotonic() + _WHOLE_TURN_TIMEOUT_SECONDS

        if on_event is not None:
            on_event({"type": "turn.started"})
        try:
            for _provider_turn in range(_MAX_PROVIDER_TURNS):
                turn = self._provider_turn_with_retry(
                    client=client,
                    profile=profile,
                    messages=messages,
                    schemas=schemas,
                    deadline=deadline,
                    on_delta=(
                        lambda text: self._record_delta(text, visible_parts, on_event)
                    ),
                )
                if not turn.tool_calls:
                    assistant = "".join(visible_parts)
                    if on_event is not None:
                        on_event({"type": "turn.completed"})
                    return ChatAgentResult(assistant=assistant, model_type=profile.model_type)

                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content or None,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {"name": call.name, "arguments": call.arguments},
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
                for call in turn.tool_calls:
                    if on_event is not None:
                        on_event(
                            {
                                "type": "turn.activity",
                                "state": "started",
                                "name": call.name or "tool",
                            }
                        )
                    output = self._execute_tool(call, tool_by_name)
                    if on_event is not None:
                        on_event(
                            {
                                "type": "turn.activity",
                                "state": "completed",
                                "name": call.name or "tool",
                            }
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": output,
                        }
                    )
            raise ChatAgentError(
                "assistant_tool_limit",
                "模型连续调用工具但没有完成回答；请缩小请求范围后重试。",
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _record_delta(text: str, visible_parts: list[str], on_event: EventSink | None) -> None:
        if not text:
            return
        visible_parts.append(text)
        if on_event is not None:
            on_event({"type": "turn.delta", "text": text})

    def _provider_turn_with_retry(
        self,
        *,
        client: Any,
        profile: ChatModelProfile,
        messages: Sequence[Mapping[str, object]],
        schemas: Sequence[Mapping[str, object]],
        deadline: float,
        on_delta: Callable[[str], None],
    ) -> _ProviderTurn:
        last_error: ChatAgentError | None = None
        for attempt in range(_MAX_PROVIDER_ATTEMPTS):
            emitted_text = False

            def capture(text: str) -> None:
                nonlocal emitted_text
                emitted_text = emitted_text or bool(text)
                on_delta(text)

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ChatAgentError(
                    "assistant_timeout",
                    "模型对话超过了交互时限；请重试，或切换到响应更快的模型。",
                    retryable=True,
                )
            try:
                return self._stream_provider_turn(
                    client=client,
                    profile=profile,
                    messages=messages,
                    schemas=schemas,
                    timeout=min(profile.request_timeout_seconds, remaining),
                    deadline=deadline,
                    monotonic=self._monotonic,
                    on_delta=capture,
                )
            except Exception as error:
                classified = _classify_provider_error(error)
                last_error = classified
                can_retry = (
                    classified.retryable
                    and not emitted_text
                    and attempt + 1 < _MAX_PROVIDER_ATTEMPTS
                    and self._monotonic() + _RETRY_DELAY_SECONDS < deadline
                )
                if not can_retry:
                    raise classified from error
                self._retry_sleep(_RETRY_DELAY_SECONDS)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _stream_provider_turn(
        *,
        client: Any,
        profile: ChatModelProfile,
        messages: Sequence[Mapping[str, object]],
        schemas: Sequence[Mapping[str, object]],
        timeout: float,
        deadline: float,
        monotonic: Callable[[], float],
        on_delta: Callable[[str], None],
    ) -> _ProviderTurn:
        request: dict[str, object] = {
            "model": profile.model_id,
            "messages": list(messages),
            "tools": list(schemas),
            "tool_choice": "auto",
            "stream": True,
            "max_tokens": profile.max_output_tokens,
            "timeout": timeout,
        }
        if profile.temperature is not None:
            request["temperature"] = profile.temperature
        if profile.extra_body:
            request["extra_body"] = dict(profile.extra_body)
        if profile.reasoning_effort:
            request["reasoning_effort"] = profile.reasoning_effort
        if profile.parallel_tool_calls is not None:
            request["parallel_tool_calls"] = profile.parallel_tool_calls

        stream = client.chat.completions.create(**request)
        content_parts: list[str] = []
        fragments: dict[int, dict[str, str]] = {}
        try:
            for chunk in stream:
                if monotonic() >= deadline:
                    raise ChatAgentError(
                        "assistant_timeout",
                        "模型对话超过了交互时限；请重试，或切换到响应更快的模型。",
                        retryable=True,
                    )
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = choices[0].delta
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    content_parts.append(content)
                    on_delta(content)
                for raw_call in getattr(delta, "tool_calls", None) or ():
                    index = int(getattr(raw_call, "index", 0) or 0)
                    fragment = fragments.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    call_id = getattr(raw_call, "id", None)
                    if isinstance(call_id, str):
                        fragment["id"] += call_id
                    function = getattr(raw_call, "function", None)
                    if function is None:
                        continue
                    name = getattr(function, "name", None)
                    if isinstance(name, str):
                        fragment["name"] += name
                    arguments = getattr(function, "arguments", None)
                    if isinstance(arguments, str):
                        fragment["arguments"] += arguments
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        tool_calls: list[_ToolCall] = []
        for index in sorted(fragments):
            fragment = fragments[index]
            call_id = fragment["id"].strip()
            name = fragment["name"].strip()
            if not call_id or not name:
                raise ChatAgentError(
                    "assistant_protocol",
                    "模型返回了不完整的工具调用；请重试，或切换其他已配置模型。",
                    retryable=True,
                )
            tool_calls.append(
                _ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=fragment["arguments"],
                )
            )
        content = "".join(content_parts)
        if not tool_calls and not content.strip():
            raise ChatAgentError(
                "assistant_protocol",
                "模型返回了空响应；请重试，或切换其他已配置模型。",
                retryable=True,
            )
        return _ProviderTurn(content=content, tool_calls=tuple(tool_calls))

    @staticmethod
    def _execute_tool(call: _ToolCall, tool_by_name: Mapping[str, _ChatTool]) -> str:
        tool = tool_by_name.get(call.name)
        if tool is None:
            return json.dumps(
                {"ok": False, "error": f"Tool {call.name!r} is not available."},
                ensure_ascii=False,
            )
        try:
            arguments = json.loads(call.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            output = tool.forward(**arguments)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return json.dumps(
                {"ok": False, "error": f"Tool input rejected: {error}"},
                ensure_ascii=False,
            )
        except Exception:
            return json.dumps(
                {"ok": False, "error": "Tool execution failed safely."},
                ensure_ascii=False,
            )
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
