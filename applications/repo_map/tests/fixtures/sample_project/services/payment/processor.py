"""Payment processor — orchestrates strategy selection and event publishing."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from ...core.cache import CacheBackend
from ...core.events import EventBus
from ...core.exceptions import AppError
from ...models.order import Order
from .strategies.base import PaymentStrategy

logger = logging.getLogger(__name__)


class PaymentError(AppError):
    """Raised when a payment cannot be processed."""


class PaymentProcessor:
    """Stateless payment facade.

    Selects a :class:`PaymentStrategy` based on the payment method,
    processes the charge, emits events, and caches receipts.

    Cross-module dependencies:
    - core.cache: receipt caching
    - core.events: order.paid / order.payment_failed events
    - core.exceptions: AppError hierarchy
    - models.order: Order dataclass
    """

    def __init__(
        self,
        strategies: dict[str, PaymentStrategy],
        cache: Optional[CacheBackend] = None,
    ):
        self._strategies = strategies
        self._cache = cache
        self._bus = EventBus()

    def process(self, order: Order, method: str = "stripe") -> dict:
        """Charge the customer and return a receipt dict."""
        strategy = self._strategies.get(method)
        if strategy is None:
            raise PaymentError(f"Unsupported payment method: {method}")

        amount = Decimal(str(order.total))
        logger.info("Processing %s payment of %s for order %s", method, amount, order.id)

        try:
            receipt = strategy.charge(order_id=order.id, amount=amount)
        except Exception as e:
            self._bus.publish("order.payment_failed", order_id=order.id, error=str(e))
            raise PaymentError(f"Payment failed for order {order.id}: {e}") from e

        self._bus.publish("order.paid", order_id=order.id, receipt=receipt)

        if self._cache:
            self._cache.set(f"receipt:{order.id}", receipt, ttl=3600)

        return receipt
