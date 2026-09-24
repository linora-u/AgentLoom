"""Shared retry, provider-call budget, and rate-limit governance for LiteLLM."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

# LiteLLM fetches its model-price catalog during import unless this is set.
# Keep the offline default local to the LiteLLM integration instead of mutating
# process state during the root agentloom import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from agentloom.execution.logging import get_logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

logger = get_logger(__name__)


class ProviderCallBudgetExceeded(RuntimeError):
    """Raised before a provider request would exceed the active call budget."""


@dataclass
class ProviderCallBudget:
    """Mutable request count scoped to one explicit execution context."""

    max_calls: int
    calls: int = 0
    provider_boundary_observed: bool = False


_PROVIDER_CALL_BUDGET: ContextVar[ProviderCallBudget | None] = ContextVar(
    "agentloom_provider_call_budget",
    default=None,
)


@dataclass
class _ModelTraceTurn:
    turn_id: str
    attempt: int = 0


_MODEL_TRACE_TURN: ContextVar[_ModelTraceTurn | None] = ContextVar(
    "agentloom_litellm_model_trace_turn", default=None,
)


@contextmanager
def bind_model_trace_turn(turn_id: str | None) -> Iterator[None]:
    token = _MODEL_TRACE_TURN.set(_ModelTraceTurn(turn_id) if turn_id else None)
    try:
        yield
    finally:
        _MODEL_TRACE_TURN.reset(token)


@contextmanager
def limit_provider_calls(max_calls: int) -> Iterator[ProviderCallBudget]:
    """Fence actual wrapped provider requests in the current context."""

    max_calls = int(max_calls)
    if max_calls < 1:
        raise ValueError("provider call budget must be positive")
    budget = ProviderCallBudget(max_calls=max_calls)
    token = _PROVIDER_CALL_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _PROVIDER_CALL_BUDGET.reset(token)


def _call_provider(
    original_func: Callable,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Count and fence one request immediately before provider invocation."""

    budget = _PROVIDER_CALL_BUDGET.get()
    if budget is not None:
        budget.provider_boundary_observed = True
        if budget.calls >= budget.max_calls:
            raise ProviderCallBudgetExceeded("provider call budget exhausted")
        budget.calls += 1
    turn = _MODEL_TRACE_TURN.get()
    recorder = None
    observed_client: Any = None
    capture_error = None
    if turn is not None:
        from agentloom.execution.observability import TraceStorageError, get_current_trace_recorder

        recorder = get_current_trace_recorder()
        turn.attempt += 1
        if recorder is not None:
            request = kwargs if not args else {"args": args, "kwargs": kwargs}
            recorder.record_model_request(
                request, runtime="smolagents", boundary="litellm_provider_call_input",
                attempt=turn.attempt, turn_id=turn.turn_id,
            )
            if (
                not args and ("messages" in kwargs or "input" in kwargs)
                and str(kwargs.get("model", "")).startswith("openai/")
                and "client" not in kwargs and kwargs.get("api_key")
            ):
                import httpx
                import litellm
                from litellm.llms.custom_httpx.http_handler import get_ssl_configuration

                def capture_http_request(request: httpx.Request) -> None:
                    nonlocal capture_error
                    try:
                        body = json.loads(request.read())
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        body = request.read().decode("utf-8", errors="replace")
                    try:
                        recorder.record_model_request(
                            {"method": request.method, "url": str(request.url),
                             "headers": dict(request.headers), "body": body},
                            runtime="smolagents", boundary="openai_http_request",
                            provider_request_complete=True, attempt=turn.attempt,
                            turn_id=turn.turn_id,
                        )
                    except TraceStorageError as exc:
                        capture_error = exc
                        raise

                if litellm.client_session is None:
                    http_client = httpx.Client(
                        verify=get_ssl_configuration(), follow_redirects=True,
                        event_hooks={"request": [capture_http_request]},
                    )
                    if "messages" in kwargs:
                        from openai import OpenAI

                        observed_client = OpenAI(
                            api_key=kwargs["api_key"], base_url=kwargs.get("api_base"),
                            http_client=http_client, max_retries=0,
                        )
                    else:
                        from litellm.llms.custom_httpx.http_handler import HTTPHandler

                        observed_client = HTTPHandler(client=http_client)
                    kwargs = {**kwargs, "client": observed_client}
    try:
        response = original_func(*args, **kwargs)
    except Exception as error:
        if recorder is not None and turn is not None:
            recorder.record_model_response(
                turn.turn_id, runtime="smolagents", error=error, attempt=turn.attempt,
            )
        if capture_error is not None:
            raise capture_error from error
        raise
    finally:
        if observed_client is not None:
            observed_client.close()
    if recorder is not None and turn is not None:
        model_dump = getattr(response, "model_dump", None)
        captured = model_dump() if callable(model_dump) else response
        recorder.record_model_response(
            turn.turn_id, captured, runtime="smolagents", attempt=turn.attempt,
        )
    return response


def _is_rate_limit_error(exception: Exception) -> bool:
    """Return whether an exception is a provider rate-limit response."""

    if isinstance(exception, RateLimitError):
        return True
    return getattr(exception, "status_code", None) == 429


def _parse_retry_after(exception: Exception) -> float | None:
    """Parse a positive Retry-After or rate-limit reset delay."""

    headers = getattr(exception, "headers", None)
    if headers is None:
        response = getattr(exception, "response", None)
        if response is not None:
            headers = getattr(response, "headers", None)
    if not headers:
        return None

    raw = None
    for header_name in (
        "retry-after",
        "Retry-After",
        "x-ratelimit-reset",
        "X-RateLimit-Reset",
        "ratelimit-reset",
        "RateLimit-Reset",
    ):
        if isinstance(headers, dict):
            raw = headers.get(header_name)
        else:
            raw = getattr(headers, "get", lambda _key: None)(header_name)
        if raw is not None:
            break
    if raw is None:
        return None

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None

    now = time.time()
    if value > now / 2:
        return max(0.0, value - now)
    return value


def is_retryable_litellm_error(exception: Exception) -> bool:
    """Return whether LiteLLM may safely retry an exception."""

    if isinstance(
        exception,
        (
            Timeout,
            RateLimitError,
            APIConnectionError,
            InternalServerError,
            ServiceUnavailableError,
            AuthenticationError,
            PermissionDeniedError,
        ),
    ):
        return True

    status_code = getattr(exception, "status_code", None)
    return bool(status_code and (status_code in {408, 409, 429} or status_code >= 500))


def create_retry_wrapper(
    original_func: Callable,
    default_num_retries: int = 3,
    default_retry_delay: float = 1.0,
    default_max_retry_delay: float = 60.0,
) -> Callable:
    """Wrap one LiteLLM provider entry point with shared governance."""

    @wraps(original_func)
    def wrapper(*args, **kwargs):
        retry_delay = kwargs.pop("retry_delay", default_retry_delay)
        max_retry_delay = kwargs.pop("max_retry_delay", default_max_retry_delay)
        num_retries = kwargs.pop("num_retries", default_num_retries)
        model_type = kwargs.pop("_agent_loom_model_type", None)

        logger.debug(
            "Retry parameters: num_retries=%s, retry_delay=%s, max_retry_delay=%s",
            num_retries,
            retry_delay,
            max_retry_delay,
        )

        if retry_delay is None or num_retries == 0:
            if num_retries == 0 or _PROVIDER_CALL_BUDGET.get() is not None:
                kwargs["num_retries"] = 0
            return _call_provider(original_func, *args, **kwargs)

        kwargs["num_retries"] = 0
        limiter = None
        state = None
        if model_type:
            try:
                from agentloom.execution.concurrency.rate_limiter import (
                    GlobalRateLimiterRegistry,
                )

                state = GlobalRateLimiterRegistry.get_state(model_type)
                limiter = GlobalRateLimiterRegistry.get_limiter(model_type)
            except Exception:
                pass

        def rate_limited_call(*call_args, **call_kwargs):
            budget = _PROVIDER_CALL_BUDGET.get()
            if budget is not None and budget.calls >= budget.max_calls:
                raise ProviderCallBudgetExceeded("provider call budget exhausted")
            if state:
                state.wait_if_limited()
            if limiter:
                limiter.throttle()
            try:
                result = _call_provider(
                    original_func,
                    *call_args,
                    **call_kwargs,
                )
                if state:
                    state.report_success()
                return result
            except Exception as exc:
                if state and _is_rate_limit_error(exc):
                    state.report_rate_limit_error(_parse_retry_after(exc))
                raise

        retryable_func = retry(
            stop=stop_after_attempt(num_retries),
            wait=wait_exponential(
                multiplier=retry_delay,
                max=max_retry_delay,
            ),
            retry=retry_if_exception(is_retryable_litellm_error),
            reraise=True,
            before_sleep=lambda retry_state: _log_retry_attempt(
                retry_state,
                num_retries,
            ),
        )(rate_limited_call)
        return retryable_func(*args, **kwargs)

    return wrapper


def _log_retry_attempt(retry_state: RetryCallState, max_retries: int) -> None:
    """Log one provider retry attempt."""

    attempt = retry_state.attempt_number
    outcome = retry_state.outcome
    exception = outcome.exception() if outcome is not None else None
    next_sleep = retry_state.next_action.sleep if retry_state.next_action else 0
    logger.warning(
        "litellm provider call failed (attempt %s/%s): %s: %s. "
        "retrying in %.2fs",
        attempt,
        max_retries,
        type(exception).__name__,
        exception,
        next_sleep,
    )


def patch_litellm_completion(litellm_module: Any) -> None:
    """Patch LiteLLM chat and Responses entry points with shared governance."""

    patched: list[str] = []
    for name in ("completion", "responses"):
        original = getattr(litellm_module, name, None)
        if original is None or hasattr(original, "_agent_loom_retry_patched"):
            continue
        wrapped = create_retry_wrapper(original)
        wrapped._agent_loom_retry_patched = True  # type: ignore[attr-defined]
        setattr(litellm_module, name, wrapped)
        patched.append(name)

    logger.debug(
        "Added custom retry mechanism for LiteLLM entry points %s "
        "(supports retry_delay and max_retry_delay parameters)",
        patched,
    )
