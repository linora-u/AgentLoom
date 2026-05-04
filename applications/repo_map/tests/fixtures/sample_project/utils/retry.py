"""Retry decorator with exponential backoff — used by services and API layer."""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Type

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """Decorator that retries a function on failure with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay cap.
        exceptions: Tuple of exception types to catch.

    Usage::

        @retry_with_backoff(max_retries=3, exceptions=(ConnectionError, TimeoutError))
        def call_external_api():
            ...
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt, max_retries, fn.__name__, delay, e,
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
