"""
Tests for src.lib.concurrency.rate_limiter — thread-safe rate limiting.

Covers:
- ThreadSafeRateLimiter: single-thread, multi-thread, RPM enforcement
- GlobalRateLimitState: 429 coordination, exponential backoff, auto-reset
- GlobalRateLimiterRegistry: per-model-type isolation, singleton, lazy init

All tests are pure Python (no LLM calls).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from src.lib.concurrency.rate_limiter import (
    GlobalRateLimiterRegistry,
    GlobalRateLimitState,
    ThreadSafeRateLimiter,
)


# ═══════════════════════════════════════════════════════════════════ #
#  Fixtures
# ═══════════════════════════════════════════════════════════════════ #

@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the global registry before each test."""
    GlobalRateLimiterRegistry.reset()
    yield
    GlobalRateLimiterRegistry.reset()


# ═══════════════════════════════════════════════════════════════════ #
#  ThreadSafeRateLimiter
# ═══════════════════════════════════════════════════════════════════ #

class TestThreadSafeRateLimiter:
    def test_interval_calculation(self):
        limiter = ThreadSafeRateLimiter(requests_per_minute=60)
        assert limiter._interval == pytest.approx(1.0, abs=0.01)

        limiter10 = ThreadSafeRateLimiter(requests_per_minute=10)
        assert limiter10._interval == pytest.approx(6.0, abs=0.01)

    def test_zero_rpm_raises(self):
        with pytest.raises(ValueError, match="positive"):
            ThreadSafeRateLimiter(requests_per_minute=0)

    def test_negative_rpm_raises(self):
        with pytest.raises(ValueError, match="positive"):
            ThreadSafeRateLimiter(requests_per_minute=-5)

    def test_single_thread_respects_interval(self):
        """Two quick calls should take at least one interval."""
        limiter = ThreadSafeRateLimiter(requests_per_minute=600)  # interval = 0.1s
        start = time.monotonic()
        limiter.throttle()
        limiter.throttle()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.09  # at least ~0.1s between calls

    def test_no_wait_when_enough_time_elapsed(self):
        limiter = ThreadSafeRateLimiter(requests_per_minute=600)
        limiter.throttle()
        time.sleep(0.15)  # > interval of 0.1s
        start = time.monotonic()
        limiter.throttle()
        elapsed = time.monotonic() - start
        assert elapsed < 0.05  # should not need to wait

    def test_multithreaded_no_burst(self):
        """5 threads calling throttle(rpm=300) — at most ~5 per second."""
        limiter = ThreadSafeRateLimiter(requests_per_minute=300)  # interval = 0.2s
        timestamps = []
        lock = threading.Lock()

        def call():
            limiter.throttle()
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(timestamps) == 5
        # Timestamps should be spaced by at least ~0.2s apart
        timestamps.sort()
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            assert gap >= 0.15, f"Gap {i}: {gap:.3f}s < 0.15s — burst detected"

    def test_thread_safety_no_exception(self):
        """100 threads concurrently throttling should not raise."""
        limiter = ThreadSafeRateLimiter(requests_per_minute=6000)
        errors = []

        def call():
            try:
                limiter.throttle()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_rpm_property(self):
        limiter = ThreadSafeRateLimiter(requests_per_minute=42)
        assert limiter.rpm == 42


# ═══════════════════════════════════════════════════════════════════ #
#  GlobalRateLimitState
# ═══════════════════════════════════════════════════════════════════ #

class TestGlobalRateLimitState:
    def test_not_limited_initially(self):
        state = GlobalRateLimitState()
        assert state.is_rate_limited is False
        assert state.consecutive_errors == 0
        # wait_if_limited should return immediately
        start = time.monotonic()
        state.wait_if_limited()
        assert time.monotonic() - start < 0.05

    def test_report_error_sets_limited(self):
        state = GlobalRateLimitState()
        state.report_rate_limit_error()
        assert state.is_rate_limited is True
        assert state.consecutive_errors == 1

    def test_wait_if_limited_sleeps(self):
        state = GlobalRateLimitState()
        # Manually set a short limit
        state.is_rate_limited = True
        state.rate_limit_reset_time = time.monotonic() + 0.3
        start = time.monotonic()
        state.wait_if_limited()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25

    def test_auto_reset_after_time_passes(self):
        state = GlobalRateLimitState()
        state.is_rate_limited = True
        state.rate_limit_reset_time = time.monotonic() - 1.0  # in the past
        state.consecutive_errors = 5
        state.wait_if_limited()
        assert state.is_rate_limited is False
        assert state.consecutive_errors == 0

    def test_consecutive_errors_exponential_backoff(self):
        state = GlobalRateLimitState()
        # Report 3 consecutive errors quickly
        state.report_rate_limit_error()
        assert state.consecutive_errors == 1
        state.report_rate_limit_error()
        assert state.consecutive_errors == 2
        state.report_rate_limit_error()
        assert state.consecutive_errors == 3
        # 3rd error: delay = 5 * 2^2 = 20s
        expected_reset = time.monotonic() + 20.0
        assert state.rate_limit_reset_time == pytest.approx(expected_reset, abs=1.0)

    def test_consecutive_errors_reset_after_gap(self):
        state = GlobalRateLimitState()
        state.report_rate_limit_error()
        assert state.consecutive_errors == 1
        # Simulate > 60s gap by setting last_error_time in the past
        state.last_error_time = time.monotonic() - 61.0
        state.report_rate_limit_error()
        assert state.consecutive_errors == 1  # reset, not 2

    def test_max_delay_cap(self):
        state = GlobalRateLimitState()
        # Force many consecutive errors
        state.consecutive_errors = 100
        state.last_error_time = time.monotonic()
        state.report_rate_limit_error()
        # delay should be capped at MAX_DELAY (300s)
        max_possible_reset = time.monotonic() + 300.0 + 1.0
        assert state.rate_limit_reset_time <= max_possible_reset

    def test_report_success_resets_counter(self):
        state = GlobalRateLimitState()
        state.consecutive_errors = 5
        state.report_success()
        assert state.consecutive_errors == 0

    def test_retry_after_takes_priority(self):
        state = GlobalRateLimitState()
        state.report_rate_limit_error(retry_after=30.0)
        expected_reset = time.monotonic() + 30.0
        assert state.rate_limit_reset_time == pytest.approx(expected_reset, abs=1.0)

    def test_multithreaded_all_wait_on_429(self):
        """One thread reports 429, others should wait."""
        state = GlobalRateLimitState()
        barrier = threading.Barrier(5)
        wait_times = []
        lock = threading.Lock()

        def reporter():
            barrier.wait()
            state.report_rate_limit_error(retry_after=0.3)

        def waiter():
            barrier.wait()
            time.sleep(0.05)  # give reporter time to update state
            start = time.monotonic()
            state.wait_if_limited()
            with lock:
                wait_times.append(time.monotonic() - start)

        threads = [threading.Thread(target=reporter)]
        threads += [threading.Thread(target=waiter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 4 waiters should have waited ~0.25s
        for wt in wait_times:
            assert wt >= 0.15, f"Waiter slept only {wt:.3f}s — should have waited"


# ═══════════════════════════════════════════════════════════════════ #
#  GlobalRateLimiterRegistry
# ═══════════════════════════════════════════════════════════════════ #

class TestGlobalRateLimiterRegistry:
    def test_get_limiter_creates_on_first_call(self):
        limiter = GlobalRateLimiterRegistry.get_limiter("powerful", 10)
        assert limiter is not None
        assert isinstance(limiter, ThreadSafeRateLimiter)

    def test_same_model_type_returns_same_instance(self):
        a = GlobalRateLimiterRegistry.get_limiter("powerful", 10)
        b = GlobalRateLimiterRegistry.get_limiter("powerful", 10)
        assert a is b

    def test_different_model_types_independent(self):
        a = GlobalRateLimiterRegistry.get_limiter("powerful", 10)
        b = GlobalRateLimiterRegistry.get_limiter("fast", 60)
        assert a is not b

    def test_get_state_creates_on_first_call(self):
        state = GlobalRateLimiterRegistry.get_state("powerful")
        assert state is not None
        assert isinstance(state, GlobalRateLimitState)

    def test_same_model_type_state_same_instance(self):
        a = GlobalRateLimiterRegistry.get_state("powerful")
        b = GlobalRateLimiterRegistry.get_state("powerful")
        assert a is b

    def test_thread_safe_lazy_init(self):
        """Multiple threads getting the same type → only one instance created."""
        results = []
        lock = threading.Lock()

        def get():
            lim = GlobalRateLimiterRegistry.get_limiter("test_type", 10)
            with lock:
                results.append(id(lim))

        threads = [threading.Thread(target=get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 1  # all same object

    def test_reset_clears_everything(self):
        GlobalRateLimiterRegistry.get_limiter("a", 10)
        GlobalRateLimiterRegistry.get_state("a")
        GlobalRateLimiterRegistry.reset()
        # After reset, new call should create fresh instances
        lim = GlobalRateLimiterRegistry.get_limiter("a", 10)
        assert lim is not None
