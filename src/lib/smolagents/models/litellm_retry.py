"""
Custom retry mechanism for litellm.

Adds exponential-backoff retry logic to `litellm.completion`, with support for
custom `retry_delay` and `max_retry_delay`.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.lib.logging import get_logger

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
    return original_func(*args, **kwargs)


# ── Rate-limit helpers (used by enhanced retry wrapper) ──────────── #


def _is_rate_limit_error(exception: Exception) -> bool:
    """Check if the exception is specifically a rate-limit (429) error."""
    if isinstance(exception, RateLimitError):
        return True
    return getattr(exception, "status_code", None) == 429


def _parse_retry_after(exception: Exception) -> float | None:
    """
    Parse Retry-After header from a 429 response.

    Also checks X-RateLimit-Reset and RateLimit-Reset headers.

    Returns:
        Wait time in seconds, or None if no valid header found.
    """
    headers = getattr(exception, "headers", None)
    if headers is None:
        # Some litellm exceptions store headers differently
        response = getattr(exception, "response", None)
        if response is not None:
            headers = getattr(response, "headers", None)
    if not headers:
        return None

    # Try multiple header names
    raw = None
    for header_name in ("retry-after", "Retry-After", "x-ratelimit-reset",
                        "X-RateLimit-Reset", "ratelimit-reset", "RateLimit-Reset"):
        if isinstance(headers, dict):
            raw = headers.get(header_name)
        else:
            raw = getattr(headers, "get", lambda _k: None)(header_name)
        if raw is not None:
            break

    if raw is None:
        return None

    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None

    if value <= 0:
        return None

    # Distinguish seconds vs Unix timestamp:
    # If value > half of current epoch time, it's likely a Unix timestamp
    now = time.time()
    if value > now / 2:
        return max(0.0, value - now)
    return value



def is_retryable_litellm_error(exception: Exception) -> bool:
    """
    Determine whether an exception is retryable for litellm.

    Uses native litellm exception types, covering all error types configured in
    `model_manager` retry policy:
    - timeout -> Timeout
    - rate_limit -> RateLimitError
    - connection_error -> APIConnectionError
    - server_error / internal_server_error -> InternalServerError
    - service_unavailable -> ServiceUnavailableError
    - authentication_error -> AuthenticationError
    - authorization_error -> PermissionDeniedError
    - bad_gateway / gateway_timeout -> status_code 502/504

    Reference:
    https://github.com/BerriAI/litellm/blob/main/litellm/router_utils/get_retry_from_policy.py

    Args:
        exception: Exception object.

    Returns:
        bool: Whether retry should be performed.
    """
    # Check litellm exception types using isinstance.
    # This is the official litellm approach.
    if isinstance(exception, (
        Timeout,                  # timeout
        RateLimitError,           # rate_limit
        APIConnectionError,       # connection_error
        InternalServerError,      # server_error, internal_server_error
        ServiceUnavailableError,  # service_unavailable
        AuthenticationError,      # authentication_error
        PermissionDeniedError,    # authorization_error
    )):
        return True

    # Check status_code for other potential errors.
    status_code = getattr(exception, "status_code", None)
    if status_code:
        # Based on litellm._should_retry logic.
        # https://github.com/BerriAI/litellm/blob/main/litellm/utils.py
        if status_code in [408, 409, 429] or status_code >= 500:
            return True

    return False


def create_retry_wrapper(
    original_func: Callable,
    default_num_retries: int = 3,
    default_retry_delay: float = 1.0,
    default_max_retry_delay: float = 60.0,
) -> Callable:
    """
    Create a wrapper function with retry logic.

    Args:
        original_func: Original function (`litellm.completion`).
        default_num_retries: Default max retry attempts.
        default_retry_delay: Default initial retry delay (seconds).
        default_max_retry_delay: Default maximum retry delay (seconds).

    Returns:
        Callable: Wrapped function.
    """

    @wraps(original_func)
    def wrapper(*args, **kwargs):
        # Extract custom retry parameters from kwargs (and remove to avoid passing to litellm).
        retry_delay = kwargs.pop("retry_delay", default_retry_delay)
        max_retry_delay = kwargs.pop("max_retry_delay", default_max_retry_delay)
        num_retries = kwargs.pop("num_retries", default_num_retries)

        # Extract AgentLoom model_type for global rate limiting (injected by LiteLLMModelV2).
        model_type = kwargs.pop("_agent_loom_model_type", None)

        logger.debug(
            f"Retry parameters: num_retries={num_retries}, "
            f"retry_delay={retry_delay}, max_retry_delay={max_retry_delay}"
        )

        # If retry params are not configured, call original function directly.
        if retry_delay is None or num_retries == 0:
            # Explicit zero must reach LiteLLM. An active budget also requires
            # zero so one wrapped call cannot hide multiple provider requests.
            # Otherwise preserve the historic retry_delay=None direct path.
            if num_retries == 0 or _PROVIDER_CALL_BUDGET.get() is not None:
                kwargs["num_retries"] = 0
            return _call_provider(original_func, *args, **kwargs)

        # Disable LiteLLM built-in retry to avoid double retry. Tenacity makes
        # each attempt observable at the provider-call budget boundary.
        kwargs["num_retries"] = 0

        # ── Build rate-limited wrapper around original_func ──
        # Global rate limiting is injected here so it's transparent to
        # the rest of the call chain (LiteLLMModelV2 unchanged).
        _limiter = None
        _state = None
        if model_type:
            try:
                from src.lib.concurrency.rate_limiter import GlobalRateLimiterRegistry
                _state = GlobalRateLimiterRegistry.get_state(model_type)
                _limiter = GlobalRateLimiterRegistry.get_limiter(model_type)
            except Exception:
                pass  # graceful degradation: skip rate limiting

        def _rate_limited_call(*a, **kw):
            # A caller-owned budget is also a cancellation boundary. Check it
            # before any global Retry-After wait or token-bucket sleep so an
            # exhausted one-call Reviewer cannot remain parked in a retry that
            # its outer fresh-Agent loop has already forbidden.
            budget = _PROVIDER_CALL_BUDGET.get()
            if budget is not None and budget.calls >= budget.max_calls:
                raise ProviderCallBudgetExceeded("provider call budget exhausted")
            # 1. Wait if another thread reported 429 (global coordination)
            if _state:
                _state.wait_if_limited()
            # 2. Per-model-type token-bucket throttle
            if _limiter:
                _limiter.throttle()
            try:
                result = _call_provider(original_func, *a, **kw)
                # 3. Report success → reset consecutive-error counter
                if _state:
                    _state.report_success()
                return result
            except Exception as exc:
                # 4. On 429 → update global state + parse Retry-After header
                if _state and _is_rate_limit_error(exc):
                    retry_after_value = _parse_retry_after(exc)
                    _state.report_rate_limit_error(retry_after_value)
                raise

        # Build retry decorator with tenacity.
        retry_decorator = retry(
            stop=stop_after_attempt(num_retries),
            wait=wait_exponential(
                multiplier=retry_delay,
                max=max_retry_delay,
            ),
            retry=retry_if_exception(is_retryable_litellm_error),
            reraise=True,
            before_sleep=lambda retry_state: _log_retry_attempt(retry_state, num_retries),
        )

        # Create retryable function — wraps rate_limited_call (not bare original_func).
        retryable_func = retry_decorator(_rate_limited_call)

        # Execute.
        return retryable_func(*args, **kwargs)

    return wrapper


def _log_retry_attempt(retry_state: RetryCallState, max_retries: int):
    """
    Log retry attempts.

    Args:
        retry_state: Tenacity retry state.
        max_retries: Maximum retry attempts.
    """
    attempt = retry_state.attempt_number
    exception = retry_state.outcome.exception()
    next_sleep = retry_state.next_action.sleep if retry_state.next_action else 0

    logger.warning(
        f"litellm.completion failed (attempt {attempt}/{max_retries}): "
        f"{type(exception).__name__}: {exception}. "
        f"retrying in {next_sleep:.2f}s"
    )


def patch_litellm_completion(litellm_module: Any):
    """
    Monkey-patch `litellm.completion` to add custom retry logic.

    Args:
        litellm_module: litellm module object.
    """
    # Check whether it has already been patched (avoid duplicate patching).
    if hasattr(litellm_module.completion, "_agent_loom_retry_patched"):
        logger.debug("litellm.completion is already patched; skipping duplicate patch")
        return

    original_completion = litellm_module.completion

    # Create wrapper function.
    wrapped_completion = create_retry_wrapper(
        original_completion
    )

    # Mark as patched.
    wrapped_completion._agent_loom_retry_patched = True

    # Replace function.
    litellm_module.completion = wrapped_completion

    logger.debug(
        "Added custom retry mechanism for litellm.completion "
        "(supports retry_delay and max_retry_delay parameters)"
    )
