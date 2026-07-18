from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

from src.tui_bridge.bridge import BridgeError, TuiBridge
from src.tui_bridge.builder import BuilderService
from src.tui_bridge.chat_agent import ChatModelProfile


def _write_catalog(project_root: Path) -> None:
    config = project_root / "config"
    config.mkdir(exist_ok=True)
    (config / "llm.yaml").write_text(
        """\
model:
  default_model_type: powerful
  powerful: &backend
    base_url: https://models.example.test/api/v3
    api_key: test-key
    model: openai/ep-test
    max_tokens: 128000
    timeout: 300
    num_retries: 10000
    extra_body:
      thinking:
        type: enabled
    tool_choice: auto
  summary:
    <<: *backend
""",
        encoding="utf-8",
    )


def _sse(*deltas: dict[str, object]) -> httpx.Response:
    events = []
    for delta in deltas:
        events.append(
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "ep-test",
                    "choices": [{"index": 0, **delta}],
                }
            )
        )
    events.append("data: [DONE]")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("\n\n".join(events) + "\n\n").encode(),
    )


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[ChatModelProfile], OpenAI]:
    transport = httpx.MockTransport(handler)

    def create(profile: ChatModelProfile) -> OpenAI:
        return OpenAI(
            api_key=profile.api_key,
            base_url=profile.base_url,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            default_headers=profile.extra_headers,
            http_client=httpx.Client(transport=transport),
        )

    return create


def test_plain_chat_uses_one_openai_compatible_stream_without_litellm_agent(
    tmp_path: Path,
) -> None:
    _write_catalog(tmp_path)
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.url.path == "/api/v3/chat/completions"
        return _sse(
            {"delta": {"role": "assistant", "content": "我是 "}, "finish_reason": None},
            {"delta": {"content": "ep-test。"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(tmp_path, chat_client_factory=_client_factory(handler))
    events: list[dict[str, object]] = []

    result = service.send(
        session_id="chat-1",
        message="你是什么模型？",
        model_type="powerful",
        on_event=events.append,
    )

    assert result["assistant"] == "我是 ep-test。"
    assert result["model_type"] == "powerful"
    assert result["draft"]["files"] == []
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == "ep-test"
    assert request["max_tokens"] == 4096
    assert request["stream"] is True
    assert request["tool_choice"] == "auto"
    assert {item["function"]["name"] for item in request["tools"]} == {
        "inspect_agent_system",
        "stage_agent_yaml",
        "validate_agent_draft",
    }
    # OpenAI SDK merges ``extra_body`` into the wire JSON.
    assert request["thinking"] == {"type": "enabled"}
    assert events == [
        {"type": "turn.started"},
        {"type": "turn.delta", "text": "我是 "},
        {"type": "turn.delta", "text": "ep-test。"},
        {"type": "turn.completed"},
    ]


def test_configured_tool_choice_none_disables_tool_schemas(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("tool_choice: auto", "tool_choice: none"),
        encoding="utf-8",
    )
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _sse(
            {"delta": {"role": "assistant", "content": "只对话"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(tmp_path, chat_client_factory=_client_factory(handler))

    result = service.send(session_id="chat-1", message="hello", model_type="powerful")

    assert result["assistant"] == "只对话"
    assert requests[0]["tool_choice"] == "none"
    assert "tools" not in requests[0]


def test_tool_choice_none_rejects_forged_provider_tool_calls_locally(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("tool_choice: auto", "tool_choice: none"),
        encoding="utf-8",
    )
    relative = "applications/reports/workflows/forged.yaml"
    target = tmp_path / relative
    arguments = json.dumps(
        {
            "path": relative,
            "content": "name: forged\ndescription: forged\nworkflow: forged\n",
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse(
            {
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-forged",
                            "type": "function",
                            "function": {
                                "name": "stage_agent_yaml",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
                "finish_reason": None,
            },
            {"delta": {}, "finish_reason": "tool_calls"},
        )

    service = BuilderService(tmp_path, chat_client_factory=_client_factory(handler))
    bridge = TuiBridge(tmp_path, builder_service=service)

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert error.value.code == "assistant_protocol"
    assert service.get_draft("chat-1")["files"] == []
    assert service.history("chat-1") == []
    assert not target.exists()


def test_invalid_tool_choice_fails_before_client_construction(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "tool_choice: auto",
            "tool_choice: arbitrary",
        ),
        encoding="utf-8",
    )
    client_created = False

    def create_client(_profile: ChatModelProfile) -> OpenAI:
        nonlocal client_created
        client_created = True
        raise AssertionError("invalid tool policy must fail before client construction")

    bridge = TuiBridge(
        tmp_path,
        builder_service=BuilderService(tmp_path, chat_client_factory=create_client),
    )

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert client_created is False
    assert error.value.code == "assistant_config"
    assert "tool_choice" in str(error.value)


def test_required_tool_choice_becomes_auto_after_the_first_tool_result(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "tool_choice: auto",
            "tool_choice: required",
        ),
        encoding="utf-8",
    )
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return _sse(
                {
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-validate",
                                "type": "function",
                                "function": {
                                    "name": "validate_agent_draft",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                },
                {"delta": {}, "finish_reason": "tool_calls"},
            )
        return _sse(
            {"delta": {"role": "assistant", "content": "检查完成"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(tmp_path, chat_client_factory=_client_factory(handler))

    result = service.send(session_id="chat-1", message="检查草稿", model_type="powerful")

    assert result["assistant"] == "检查完成"
    assert [request["tool_choice"] for request in requests] == ["required", "auto"]


def test_fragmented_tool_call_stages_only_in_memory_then_returns_plain_text(
    tmp_path: Path,
) -> None:
    _write_catalog(tmp_path)
    requests: list[dict[str, object]] = []
    yaml_content = """\
name: report_agent
description: Build a report.
model_type: powerful
tool_call_type: tool_call
workflow: |
  Return a concise report.
"""
    arguments = json.dumps(
        {
            "path": "applications/reports/workflows/report_agent.yaml",
            "content": yaml_content,
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            split = len(arguments) // 2
            return _sse(
                {
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-stage",
                                "type": "function",
                                "function": {
                                    "name": "stage_agent_yaml",
                                    "arguments": arguments[:split],
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                },
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": arguments[split:]},
                            }
                        ]
                    },
                    "finish_reason": None,
                },
                {"delta": {}, "finish_reason": "tool_calls"},
            )
        return _sse(
            {"delta": {"role": "assistant", "content": "草稿已生成，请确认后 /apply。"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(tmp_path, chat_client_factory=_client_factory(handler))
    events: list[dict[str, object]] = []

    result = service.send(
        session_id="chat-1",
        message="创建一个报告 Agent",
        on_event=events.append,
    )

    assert result["assistant"] == "草稿已生成，请确认后 /apply。"
    assert result["draft"]["valid"] is True
    assert result["draft"]["revision"] == 1
    assert not (tmp_path / "applications/reports/workflows/report_agent.yaml").exists()
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["tool_call_id"] == "call-stage"
    assert {event.get("state") for event in events if event["type"] == "turn.activity"} == {
        "started",
        "completed",
    }


def test_timeout_is_retried_once_without_committing_failed_history(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    attempts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        attempts.append(body)
        if len(attempts) <= 2:
            raise httpx.ReadTimeout("secret upstream details", request=request)
        return _sse(
            {"delta": {"role": "assistant", "content": "恢复了"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(
        tmp_path,
        chat_client_factory=_client_factory(handler),
        retry_sleep=lambda _seconds: None,
    )
    bridge = TuiBridge(tmp_path, builder_service=service)

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "失败的消息", "model_type": "powerful"},
        )

    assert error.value.code == "assistant_timeout"
    assert "secret upstream details" not in str(error.value)
    assert service.history("chat-1") == []

    recovered = bridge.dispatch(
        "assistant.send",
        {"session_id": "chat-1", "message": "新的消息", "model_type": "powerful"},
    )

    assert recovered["assistant"] == "恢复了"
    assert [message["content"] for message in attempts[-1]["messages"] if message["role"] == "user"] == [
        "新的消息"
    ]


def test_empty_provider_stream_is_retried_once_before_failing_the_turn(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse({"delta": {"role": "assistant"}, "finish_reason": "stop"})
        return _sse(
            {"delta": {"role": "assistant", "content": "第二次有内容"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(
        tmp_path,
        chat_client_factory=_client_factory(handler),
        retry_sleep=lambda _seconds: None,
    )

    result = service.send(session_id="chat-1", message="hello", model_type="powerful")

    assert calls == 2
    assert result["assistant"] == "第二次有内容"


def test_short_retry_after_is_respected_and_retry_is_visible(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    calls = 0
    delays: list[float] = []
    events: list[dict[str, object]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "3"},
                json={"error": {"message": "rate limited"}},
            )
        return _sse(
            {"delta": {"role": "assistant", "content": "已恢复"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        )

    service = BuilderService(
        tmp_path,
        chat_client_factory=_client_factory(handler),
        retry_sleep=delays.append,
    )

    result = service.send(
        session_id="chat-1",
        message="hello",
        model_type="powerful",
        on_event=events.append,
    )

    assert result["assistant"] == "已恢复"
    assert delays == [3.0]
    assert {event.get("state") for event in events if event.get("name") == "模型重试 2/2"} == {
        "started",
        "completed",
    }


def test_long_retry_after_returns_rate_limit_without_retrying_early(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    calls = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"retry-after": "30"},
            json={"error": {"message": "rate limited"}},
        )

    bridge = TuiBridge(
        tmp_path,
        builder_service=BuilderService(
            tmp_path,
            chat_client_factory=_client_factory(handler),
            retry_sleep=delays.append,
        ),
    )

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert error.value.code == "assistant_rate_limit"
    assert calls == 1
    assert delays == []


def test_auth_error_is_not_retried_and_is_safe_for_the_rpc(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "secret credential detail"}})

    service = BuilderService(tmp_path, chat_client_factory=_client_factory(handler))
    bridge = TuiBridge(tmp_path, builder_service=service)

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert calls == 1
    assert error.value.code == "assistant_auth"
    assert "config/llm.yaml" in str(error.value)
    assert "secret credential detail" not in str(error.value)


@pytest.mark.parametrize("invalid_base_url", ["not-a-url", "http://models.example.test/api/v3"])
def test_invalid_openai_compatible_base_url_is_a_local_config_error(
    tmp_path: Path,
    invalid_base_url: str,
) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "https://models.example.test/api/v3",
            invalid_base_url,
        ),
        encoding="utf-8",
    )
    client_created = False

    def create_client(_profile: ChatModelProfile) -> OpenAI:
        nonlocal client_created
        client_created = True
        raise AssertionError("invalid configuration must fail before client construction")

    bridge = TuiBridge(
        tmp_path,
        builder_service=BuilderService(tmp_path, chat_client_factory=create_client),
    )

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert client_created is False
    assert error.value.code == "assistant_config"
    assert "base_url" in str(error.value)


def test_custom_endpoint_never_receives_an_ambient_openai_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "api_key: test-key",
            'api_key: ""',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret-must-not-be-used")
    client_created = False

    def create_client(_profile: ChatModelProfile) -> OpenAI:
        nonlocal client_created
        client_created = True
        raise AssertionError("missing project credentials must fail before client construction")

    bridge = TuiBridge(
        tmp_path,
        builder_service=BuilderService(tmp_path, chat_client_factory=create_client),
    )

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert client_created is False
    assert error.value.code == "assistant_config"
    assert "凭据" in str(error.value)
    assert "ambient-secret-must-not-be-used" not in str(error.value)


def test_default_openai_endpoint_rejects_non_openai_litellm_model_prefix(tmp_path: Path) -> None:
    _write_catalog(tmp_path)
    config_path = tmp_path / "config" / "llm.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace("    base_url: https://models.example.test/api/v3\n", "")
        .replace("model: openai/ep-test", "model: anthropic/claude-test"),
        encoding="utf-8",
    )
    client_created = False

    def create_client(_profile: ChatModelProfile) -> OpenAI:
        nonlocal client_created
        client_created = True
        raise AssertionError("unsupported provider must fail before client construction")

    bridge = TuiBridge(
        tmp_path,
        builder_service=BuilderService(tmp_path, chat_client_factory=create_client),
    )

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "assistant.send",
            {"session_id": "chat-1", "message": "hello", "model_type": "powerful"},
        )

    assert client_created is False
    assert error.value.code == "assistant_config"
    assert "OpenAI-compatible" in str(error.value)


def test_importing_tui_chat_does_not_import_litellm_or_smolagents() -> None:
    script = """
import sys
import src.tui_bridge.builder
import src.tui_bridge.chat_agent
loaded = sorted(name for name in sys.modules if name == 'litellm' or name.startswith('smolagents'))
if loaded:
    raise SystemExit(','.join(loaded))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
