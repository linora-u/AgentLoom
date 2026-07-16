from __future__ import annotations

from collections.abc import Callable

import click
import httpx
import pytest
from click.testing import CliRunner
from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from smolagents import (
    AgentGenerationError,
    AgentLogger,
    AgentMaxStepsError,
    AgentParsingError,
)

from src.__main__ import main
from src.lib.smolagents.models.litellm_retry import ProviderCallBudgetExceeded
from src.lib.smolagents.models.tool_call_parser import ToolCallParseError


def _wrapped_generation_failure(provider_error: Exception) -> RuntimeError:
    generation_error = AgentGenerationError("generation failed", AgentLogger())
    generation_error.__cause__ = provider_error
    outer = RuntimeError("outer runtime failure")
    outer.__cause__ = generation_error
    return outer


def _invoke_failure(monkeypatch: pytest.MonkeyPatch, error: BaseException):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("src.runner.run_app", fail)
    return CliRunner().invoke(main, ["run", "unused.yaml"])


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: Timeout(
            "provider timed out",
            model="summary",
            llm_provider="openai",
        ),
        lambda: APIConnectionError(
            "provider connection failed",
            model="summary",
            llm_provider="openai",
        ),
        lambda: InternalServerError(
            "provider internal error",
            model="summary",
            llm_provider="openai",
        ),
        lambda: ServiceUnavailableError(
            "provider unavailable",
            model="summary",
            llm_provider="openai",
        ),
        lambda: RateLimitError(
            "provider rate limited",
            model="summary",
            llm_provider="openai",
        ),
    ],
    ids=["timeout", "connection", "internal", "unavailable", "rate-limit"],
)
def test_run_uses_tempfail_for_nested_transient_litellm_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Callable[[], Exception],
) -> None:
    error = _wrapped_generation_failure(error_factory())

    result = _invoke_failure(monkeypatch, error)

    assert result.exit_code == 75
    assert "Execution failed: outer runtime failure" in result.output


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: AgentParsingError("invalid model output", AgentLogger()),
        lambda: AgentMaxStepsError("maximum steps reached", AgentLogger()),
        lambda: _wrapped_generation_failure(
            AuthenticationError(
                "invalid credential",
                model="summary",
                llm_provider="openai",
            )
        ),
        lambda: _wrapped_generation_failure(
            PermissionDeniedError(
                "permission denied",
                model="summary",
                llm_provider="openai",
                response=httpx.Response(
                    403,
                    request=httpx.Request("POST", "https://provider.invalid"),
                ),
            )
        ),
        lambda: _wrapped_generation_failure(
            BadRequestError(
                "invalid request",
                model="summary",
                llm_provider="openai",
            )
        ),
        lambda: ProviderCallBudgetExceeded("provider call budget exhausted"),
        lambda: ToolCallParseError("invalid tool call"),
        lambda: TimeoutError("raw timeout"),
        lambda: ConnectionError("raw connection error"),
        lambda: click.UsageError("invalid CLI usage"),
        lambda: click.exceptions.Exit(75),
        lambda: RuntimeError("RateLimitError status_code=503"),
    ],
    ids=[
        "parse",
        "max-steps",
        "authentication",
        "permission",
        "bad-request",
        "provider-budget",
        "tool-call-parse",
        "raw-timeout",
        "raw-connection",
        "click-usage",
        "nested-click-exit",
        "error-like-text",
    ],
)
def test_run_keeps_semantic_and_non_transient_failures_at_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    error_factory: Callable[[], Exception],
) -> None:
    result = _invoke_failure(monkeypatch, error_factory())

    assert result.exit_code == 1


@pytest.mark.parametrize(
    "denied",
    [
        AgentParsingError("semantic parse failure", AgentLogger()),
        ProviderCallBudgetExceeded("provider call budget exhausted"),
        ToolCallParseError("invalid tool call"),
    ],
    ids=["agent-parse", "provider-budget", "tool-call-parse"],
)
def test_denied_error_wins_over_transient_error_in_same_chain(
    monkeypatch: pytest.MonkeyPatch,
    denied: Exception,
) -> None:
    denied.__cause__ = Timeout(
        "provider timed out",
        model="summary",
        llm_provider="openai",
    )

    result = _invoke_failure(monkeypatch, denied)

    assert result.exit_code == 1


def test_suppressed_context_cannot_authorize_tempfail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("semantic failure")
    error.__context__ = Timeout(
        "provider timed out",
        model="summary",
        llm_provider="openai",
    )
    error.__suppress_context__ = True

    result = _invoke_failure(monkeypatch, error)

    assert result.exit_code == 1


def test_unsuppressed_context_can_authorize_tempfail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("outer runtime failure")
    error.__context__ = Timeout(
        "provider timed out",
        model="summary",
        llm_provider="openai",
    )

    result = _invoke_failure(monkeypatch, error)

    assert result.exit_code == 75


def test_explicit_cause_takes_priority_over_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("outer runtime failure")
    error.__cause__ = Timeout(
        "provider timed out",
        model="summary",
        llm_provider="openai",
    )
    error.__context__ = AgentParsingError("ignored context", AgentLogger())

    result = _invoke_failure(monkeypatch, error)

    assert result.exit_code == 75


def test_exception_cycle_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first

    result = _invoke_failure(monkeypatch, first)

    assert result.exit_code == 1


def test_exception_cycle_with_transient_member_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transient = Timeout(
        "provider timed out",
        model="summary",
        llm_provider="openai",
    )
    wrapper = RuntimeError("wrapper")
    transient.__cause__ = wrapper
    wrapper.__cause__ = transient

    result = _invoke_failure(monkeypatch, wrapper)

    assert result.exit_code == 1


def test_nested_system_exit_cannot_forge_tempfail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_failure(monkeypatch, SystemExit(75))

    assert result.exit_code == 1


def test_wrapped_system_exit_cannot_forge_tempfail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("outer runtime failure")
    error.__cause__ = SystemExit(75)

    result = _invoke_failure(monkeypatch, error)

    assert result.exit_code == 1


def test_nested_system_exit_detail_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_failure(monkeypatch, SystemExit("sensitive-provider-detail"))

    assert result.exit_code == 1
    assert "nested process exit" in result.output
    assert "sensitive-provider-detail" not in result.output


def test_keyboard_interrupt_keeps_shell_interrupt_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_failure(monkeypatch, KeyboardInterrupt())

    assert result.exit_code == 130
