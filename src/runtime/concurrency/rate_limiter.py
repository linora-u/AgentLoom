"""
Thread-safe rate limiting for concurrent LLM API calls.

Provides:
- ThreadSafeRateLimiter: Lock-protected minimum-interval limiter (replaces smolagents' non-thread-safe version)
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class ThreadSafeRateLimiter:
    """
    Thread-safe minimum-interval rate limiter.

    Ensures at most ``requests_per_minute`` calls per minute by enforcing
    a minimum interval between consecutive calls. Uses ``threading.Lock``
    to prevent race conditions that exist in smolagents' native RateLimiter.

    Unlike a token-bucket, this is simple and deterministic: each ``throttle()``
    call sleeps until the minimum interval has elapsed since the last call.
    """

    def __init__(self, requests_per_minute: int = 10):
        if requests_per_minute <= 0:
            raise ValueError(f"requests_per_minute must be positive, got {requests_per_minute}")
        self._rpm = requests_per_minute
        self._interval = 60.0 / requests_per_minute
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    @property
    def rpm(self) -> int:
        return self._rpm

    def throttle(self) -> None:
        """Block until the minimum interval has elapsed since the last call."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait = self._interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()


class GlobalRateLimitState:
    """
    Shared 429-error coordination state.

    When ANY thread receives a 429 (rate-limit) error, it calls
    ``report_rate_limit_error()`` which updates global state. ALL other
    threads then automatically wait via ``wait_if_limited()`` before
    making their next request.

    Backoff strategy:
    - Consecutive 429 errors within 60s window → exponential backoff
    - Base delay: 5s, multiplied by 2^(consecutive_errors - 1)
    - Max delay: 300s (5 minutes)
    - If server provides Retry-After header, that takes priority

    Thread safety: all state mutations protected by ``threading.Lock``.
    """

    # Backoff configuration
    BASE_DELAY: float = 5.0
    MAX_DELAY: float = 300.0
    CONSECUTIVE_WINDOW: float = 60.0  # seconds

    def __init__(self):
        self.is_rate_limited: bool = False
        self.rate_limit_reset_time: float = 0.0  # monotonic timestamp
        self.consecutive_errors: int = 0
        self.last_error_time: float = 0.0  # monotonic timestamp
        self._lock = threading.Lock()

    def wait_if_limited(self) -> None:
        """Check global rate-limit state; sleep if currently limited."""
        # Fast path: no lock needed if not limited
        if not self.is_rate_limited:
            return

        wait_time = 0.0
        with self._lock:
            now = time.monotonic()
            if self.is_rate_limited and self.rate_limit_reset_time > now:
                wait_time = self.rate_limit_reset_time - now
            elif self.is_rate_limited:
                # Time has passed → auto-reset
                self.is_rate_limited = False
                self.consecutive_errors = 0
                return

        # Sleep outside the lock so other threads can still read state
        if wait_time > 0:
            time.sleep(wait_time)

    def report_rate_limit_error(self, retry_after: Optional[float] = None) -> None:
        """
        Called when a thread receives a 429 error.

        Updates global state so all threads wait before retrying.

        Args:
            retry_after: Server-suggested wait time in seconds (from Retry-After header).
                         If provided and positive, takes priority over exponential backoff.
        """
        with self._lock:
            now = time.monotonic()
            # Consecutive errors: only accumulate within 60s window
            if now - self.last_error_time < self.CONSECUTIVE_WINDOW:
                self.consecutive_errors += 1
            else:
                self.consecutive_errors = 1
            self.last_error_time = now

            # Calculate delay
            if retry_after is not None and retry_after > 0:
                delay = retry_after
            else:
                delay = min(
                    self.BASE_DELAY * (2 ** (self.consecutive_errors - 1)),
                    self.MAX_DELAY,
                )

            self.is_rate_limited = True
            self.rate_limit_reset_time = now + delay

    def report_success(self) -> None:
        """Called after a successful API call. Resets consecutive error counter."""
        if self.consecutive_errors > 0:
            with self._lock:
                self.consecutive_errors = 0


class GlobalRateLimiterRegistry:
    """
    Singleton registry that maintains per-model-type limiters and rate-limit states.

    Each model_type (e.g. "powerful", "fast", "summary") gets its own
    independent ThreadSafeRateLimiter and GlobalRateLimitState, so
    rate-limit errors on one model type don't affect others.

    Thread-safe: lazy initialization protected by ``threading.Lock``.
    """

    _lock = threading.Lock()
    _limiters: dict[str, ThreadSafeRateLimiter] = {}
    _states: dict[str, GlobalRateLimitState] = {}

    @classmethod
    def get_limiter(cls, model_type: str, rpm: int = 10) -> ThreadSafeRateLimiter:
        """Get or create a rate limiter for the given model type."""
        # Fast path without lock
        limiter = cls._limiters.get(model_type)
        if limiter is not None:
            return limiter

        with cls._lock:
            # Double-check after acquiring lock
            if model_type not in cls._limiters:
                cls._limiters[model_type] = ThreadSafeRateLimiter(rpm)
            return cls._limiters[model_type]

    @classmethod
    def get_state(cls, model_type: str) -> GlobalRateLimitState:
        """Get or create a rate-limit state for the given model type."""
        state = cls._states.get(model_type)
        if state is not None:
            return state

        with cls._lock:
            if model_type not in cls._states:
                cls._states[model_type] = GlobalRateLimitState()
            return cls._states[model_type]

    @classmethod
    def reset(cls) -> None:
        """Reset all limiters and states. Primarily for testing."""
        with cls._lock:
            cls._limiters.clear()
            cls._states.clear()
