"""
Tests for the enhanced litellm_retry.py — global rate limiting integration.

Covers:
- _parse_retry_after(): Retry-After header parsing
- _is_rate_limit_error(): 429 detection
- create_retry_wrapper(): global limiter injection, 429 coordination, backward compat

All tests use mocks (no real LLM calls).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from litellm.exceptions import RateLimitError, Timeout

from src.lib.concurrency.rate_limiter import GlobalRateLimiterRegistry
from src.lib.smolagents.models.litellm_retry import (
    ProviderCallBudgetExceeded,
    _is_rate_limit_error,
    _parse_retry_after,
    create_retry_wrapper,
    limit_provider_calls,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    GlobalRateLimiterRegistry.reset()
    yield
    GlobalRateLimiterRegistry.reset()


# ═══════════════════════════════════════════════════════════════════ #
#  _parse_retry_after
# ═══════════════════════════════════════════════════════════════════ #

class TestParseRetryAfter:
    def test_parse_seconds_format(self):
        exc = MagicMock()
        exc.headers = {"retry-after": "30"}
        assert _parse_retry_after(exc) == pytest.approx(30.0)

    def test_parse_x_ratelimit_reset(self):
        exc = MagicMock()
        exc.headers = {"x-ratelimit-reset": "45"}
        assert _parse_retry_after(exc) == pytest.approx(45.0)

    def test_no_headers_returns_none(self):
        exc = MagicMock(spec=[])  # no headers attr
        assert _parse_retry_after(exc) is None

    def test_empty_headers_returns_none(self):
        exc = MagicMock()
        exc.headers = {}
        assert _parse_retry_after(exc) is None

    def test_invalid_value_returns_none(self):
        exc = MagicMock()
        exc.headers = {"retry-after": "abc"}
        assert _parse_retry_after(exc) is None

    def test_negative_value_returns_none(self):
        exc = MagicMock()
        exc.headers = {"retry-after": "-5"}
        assert _parse_retry_after(exc) is None

    def test_unix_timestamp_format(self):
        future = time.time() + 60.0
        exc = MagicMock()
        exc.headers = {"retry-after": str(future)}
        result = _parse_retry_after(exc)
        assert result is not None
        assert result == pytest.approx(60.0, abs=2.0)

    def test_header_priority_retry_after_first(self):
        exc = MagicMock()
        exc.headers = {"retry-after": "10", "x-ratelimit-reset": "99"}
        assert _parse_retry_after(exc) == pytest.approx(10.0)


# ═══════════════════════════════════════════════════════════════════ #
#  _is_rate_limit_error
# ═══════════════════════════════════════════════════════════════════ #

class TestIsRateLimitError:
    def test_rate_limit_error_true(self):
        exc = RateLimitError(message="rate limited", model="test", llm_provider="test")
        assert _is_rate_limit_error(exc) is True

    def test_status_code_429_true(self):
        exc = MagicMock()
        exc.status_code = 429
        assert _is_rate_limit_error(exc) is True

    def test_other_error_false(self):
        assert _is_rate_limit_error(ValueError("oops")) is False

    def test_timeout_false(self):
        exc = Timeout(message="timeout", model="test", llm_provider="test")
        assert _is_rate_limit_error(exc) is False


# ═══════════════════════════════════════════════════════════════════ #
#  create_retry_wrapper — global rate limiting integration
# ═══════════════════════════════════════════════════════════════════ #

class TestRetryWrapperGlobalRateLimit:
    def test_exhausted_provider_budget_fails_before_global_retry_wait(self, monkeypatch):
        provider_calls = 0
        state = MagicMock()
        limiter = MagicMock()
        monkeypatch.setattr(GlobalRateLimiterRegistry, "get_state", lambda _model_type: state)
        monkeypatch.setattr(GlobalRateLimiterRegistry, "get_limiter", lambda _model_type: limiter)

        def failing_completion(**kwargs):
            nonlocal provider_calls
            provider_calls += 1
            raise Timeout(message="timeout", model="test", llm_provider="test")

        wrapper = create_retry_wrapper(
            failing_completion,
            default_num_retries=10,
            default_retry_delay=0.0,
            default_max_retry_delay=0.0,
        )

        with limit_provider_calls(1):
            with pytest.raises(ProviderCallBudgetExceeded):
                wrapper(model="test", _agent_loom_model_type="powerful")

        assert provider_calls == 1
        assert state.wait_if_limited.call_count == 1
        assert limiter.throttle.call_count == 1

    def test_provider_budget_stops_tenacity_before_a_fifth_request(self):
        """One review budget fences retry attempts at the provider boundary."""
        provider_calls = 0

        def failing_completion(**kwargs):
            nonlocal provider_calls
            provider_calls += 1
            assert kwargs["num_retries"] == 0
            raise Timeout(message="timeout", model="test", llm_provider="test")

        wrapper = create_retry_wrapper(
            failing_completion,
            default_num_retries=10,
            default_retry_delay=0.0,
            default_max_retry_delay=0.0,
        )

        with limit_provider_calls(4) as budget:
            with pytest.raises(ProviderCallBudgetExceeded):
                wrapper(model="test")

        assert provider_calls == 4
        assert budget.calls == 4

    def test_zero_retries_is_forwarded_to_litellm(self):
        """The direct path must disable LiteLLM's own hidden retry loop."""
        received = {}

        def fake_completion(**kwargs):
            received.update(kwargs)
            return "ok"

        wrapper = create_retry_wrapper(fake_completion)

        with limit_provider_calls(4) as budget:
            assert wrapper(model="test", num_retries=0) == "ok"

        assert received["num_retries"] == 0
        assert budget.calls == 1

    def test_retry_delay_none_without_budget_preserves_direct_call_semantics(self):
        """The review fence must not change unrelated direct model calls."""
        received = {}

        def fake_completion(**kwargs):
            received.update(kwargs)
            return "ok"

        wrapper = create_retry_wrapper(fake_completion)

        assert wrapper(model="test", num_retries=7, retry_delay=None) == "ok"

        assert "num_retries" not in received

    def test_active_budget_disables_hidden_retry_on_direct_call(self):
        received = {}

        def fake_completion(**kwargs):
            received.update(kwargs)
            return "ok"

        wrapper = create_retry_wrapper(fake_completion)

        with limit_provider_calls(4):
            assert wrapper(
                model="test",
                num_retries=7,
                retry_delay=None,
            ) == "ok"

        assert received["num_retries"] == 0

    def test_model_type_popped_from_kwargs(self):
        """_agent_loom_model_type should be removed before calling litellm."""
        received = {}

        def fake_completion(**kwargs):
            received.update(kwargs)
            return "ok"

        wrapper = create_retry_wrapper(fake_completion)
        wrapper(
            model="test",
            _agent_loom_model_type="powerful",
            retry_delay=1.0,
            num_retries=1,
        )
        assert "_agent_loom_model_type" not in received

    def test_no_model_type_works(self):
        """Without _agent_loom_model_type, should still work (backward compat)."""
        def fake_completion(**kwargs):
            return "ok"

        wrapper = create_retry_wrapper(fake_completion)
        result = wrapper(model="test", retry_delay=1.0, num_retries=1)
        assert result == "ok"

    def test_report_success_called(self):
        """On success, state.report_success() should be called."""
        def fake_completion(**kwargs):
            return "ok"

        wrapper = create_retry_wrapper(fake_completion)
        wrapper(model="test", _agent_loom_model_type="powerful", retry_delay=1.0, num_retries=1)

        state = GlobalRateLimiterRegistry.get_state("powerful")
        # After success, consecutive_errors should be 0
        assert state.consecutive_errors == 0

    def test_report_error_on_429(self):
        """On RateLimitError, state should record the error."""
        call_count = 0

        def failing_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            raise RateLimitError(message="rate limited", model="test", llm_provider="test")

        wrapper = create_retry_wrapper(failing_completion, default_num_retries=2, default_retry_delay=0.01, default_max_retry_delay=0.02)

        with pytest.raises(RateLimitError):
            wrapper(model="test", _agent_loom_model_type="fast")

        state = GlobalRateLimiterRegistry.get_state("fast")
        assert state.is_rate_limited is True
        assert state.consecutive_errors >= 1

    def test_original_retry_logic_preserved(self):
        """Non-429 retryable errors should still use tenacity retry."""
        call_count = 0

        def flaky_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Timeout(message="timeout", model="test", llm_provider="test")
            return "ok"

        wrapper = create_retry_wrapper(flaky_completion, default_num_retries=5, default_retry_delay=0.01, default_max_retry_delay=0.02)
        result = wrapper(model="test", _agent_loom_model_type="powerful")
        assert result == "ok"
        assert call_count == 3  # 2 failures + 1 success
