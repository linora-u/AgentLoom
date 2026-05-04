"""Central event bus with sync/async dispatch and wildcard subscriptions."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

Listener = Callable[..., None]


class EventBus:
    """Process-global event bus (singleton).

    Supports:
    - Exact topic subscriptions: ``bus.subscribe("order.created", fn)``
    - Wildcard subscriptions: ``bus.subscribe("order.*", fn)``
    - Synchronous dispatch with error isolation per listener
    """

    _instance: EventBus | None = None
    _lock = threading.Lock()

    def __new__(cls) -> EventBus:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._listeners: Dict[str, List[Listener]] = defaultdict(list)
                inst._stats = {"published": 0, "delivered": 0, "errors": 0}
                cls._instance = inst
            return cls._instance

    def subscribe(self, topic: str, listener: Listener) -> None:
        """Register a listener for a topic (exact or wildcard ``*``)."""
        self._listeners[topic].append(listener)
        logger.debug("Subscribed %s to '%s'", listener.__name__, topic)

    def unsubscribe(self, topic: str, listener: Listener) -> bool:
        """Remove a listener. Returns True if found."""
        listeners = self._listeners.get(topic, [])
        try:
            listeners.remove(listener)
            return True
        except ValueError:
            return False

    def publish(self, topic: str, **payload: Any) -> int:
        """Dispatch an event to all matching listeners.

        Returns number of listeners that received the event.
        """
        self._stats["published"] += 1
        targets = list(self._listeners.get(topic, []))

        # Wildcard: "order.*" matches "order.created"
        prefix = topic.rsplit(".", 1)[0] if "." in topic else ""
        if prefix:
            targets.extend(self._listeners.get(f"{prefix}.*", []))

        delivered = 0
        for fn in targets:
            try:
                fn(topic=topic, **payload)
                delivered += 1
            except Exception:
                self._stats["errors"] += 1
                logger.exception("Listener %s failed on '%s'", fn.__name__, topic)

        self._stats["delivered"] += delivered
        return delivered

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def reset(self) -> None:
        """Clear all subscriptions (for testing)."""
        self._listeners.clear()
        self._stats = {"published": 0, "delivered": 0, "errors": 0}
