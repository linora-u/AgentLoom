"""
Monkey-patch to disable smolagents' native (non-thread-safe) RateLimiter.

Problem:
    smolagents.utils.RateLimiter uses a bare ``time.sleep`` + ``_last_call``
    timestamp **without any locking**. Under concurrent ThreadPoolExecutor
    usage (parallel Agent-as-Tool calls) this causes race conditions:
    multiple threads read the same ``_last_call``, all compute the same
    short sleep, and burst through the rate limit simultaneously.

Fix:
    Replace ``RateLimiter.throttle`` with a no-op. Actual thread-safe
    rate limiting is handled by ``src.lib.concurrency.rate_limiter``
    integrated into the ``litellm_retry`` wrapper.
"""

from __future__ import annotations

import warnings

_PATCHED = False


def patch_rate_limiter() -> None:
    """
    Replace ``smolagents.utils.RateLimiter.throttle`` with a no-op.

    Safe to call multiple times (idempotent).
    """
    global _PATCHED
    if _PATCHED:
        return

    try:
        from smolagents.utils import RateLimiter
    except ImportError:
        warnings.warn(
            "smolagents.utils.RateLimiter not found; rate_limiter_patch skipped",
            stacklevel=2,
        )
        return

    # Store original for potential restoration in tests
    if not hasattr(RateLimiter, "_original_throttle"):
        RateLimiter._original_throttle = RateLimiter.throttle

    def _noop_throttle(self):  # noqa: ARG001
        """No-op replacement. Global rate limiting handled by AgentLoom concurrency module."""
        pass

    RateLimiter.throttle = _noop_throttle
    _PATCHED = True
