"""
Tests for src.lib.smolagents.monkey_patch.rate_limiter_patch.

Verifies that patching makes RateLimiter.throttle a no-op.
"""

from __future__ import annotations

import time

import pytest


class TestRateLimiterPatch:
    def test_patch_makes_throttle_noop(self):
        """After patching, throttle() should return instantly."""
        from smolagents.utils import RateLimiter

        from src.lib.smolagents.monkey_patch.rate_limiter_patch import patch_rate_limiter
        patch_rate_limiter()

        rl = RateLimiter(requests_per_minute=1)  # 1 RPM → 60s interval normally
        start = time.monotonic()
        for _ in range(10):
            rl.throttle()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"throttle should be no-op but took {elapsed:.2f}s"

    def test_patch_is_idempotent(self):
        """Calling patch_rate_limiter() twice should not raise."""
        from src.lib.smolagents.monkey_patch.rate_limiter_patch import patch_rate_limiter
        patch_rate_limiter()
        patch_rate_limiter()  # should not raise

    def test_original_stored(self):
        """After patching, RateLimiter should have _original_throttle."""
        from smolagents.utils import RateLimiter

        from src.lib.smolagents.monkey_patch.rate_limiter_patch import patch_rate_limiter
        patch_rate_limiter()

        assert hasattr(RateLimiter, "_original_throttle")
        assert callable(RateLimiter._original_throttle)
