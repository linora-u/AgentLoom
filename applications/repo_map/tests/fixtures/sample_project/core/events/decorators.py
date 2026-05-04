"""Decorator for auto-subscribing functions to the event bus."""

from __future__ import annotations

from functools import wraps
from typing import Callable

from .bus import EventBus


def on_event(topic: str) -> Callable:
    """Decorator that subscribes a function to ``topic`` on import.

    Usage::

        @on_event("order.created")
        def send_confirmation_email(topic: str, order_id: int, **kw):
            ...
    """

    def decorator(fn: Callable) -> Callable:
        EventBus().subscribe(topic, fn)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return wrapper

    return decorator
