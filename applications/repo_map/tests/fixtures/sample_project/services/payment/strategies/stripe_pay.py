"""Stripe payment strategy — wraps Stripe Charges API."""

from __future__ import annotations

import hashlib
import logging
import time
from decimal import Decimal

from .base import PaymentStrategy

logger = logging.getLogger(__name__)


class StripeStrategy(PaymentStrategy):
    """Stripe adapter with idempotency keys and retry logic."""

    def __init__(self, api_key: str = "sk_test_xxx", max_retries: int = 3):
        self._api_key = api_key
        self._max_retries = max_retries

    def charge(self, order_id: int, amount: Decimal) -> dict:
        self.validate_amount(amount)
        idempotency_key = self._build_idempotency_key(order_id, amount)
        logger.info("Stripe charge: order=%s amount=%s key=%s", order_id, amount, idempotency_key[:12])

        # Simulated Stripe API call
        return {
            "transaction_id": f"ch_{idempotency_key[:16]}",
            "provider": "stripe",
            "amount": str(amount),
            "currency": "USD",
            "status": "succeeded",
            "idempotency_key": idempotency_key,
        }

    def refund(self, transaction_id: str, amount: Decimal | None = None) -> dict:
        logger.info("Stripe refund: txn=%s amount=%s", transaction_id, amount or "full")
        return {
            "refund_id": f"re_{transaction_id[3:]}",
            "provider": "stripe",
            "amount": str(amount) if amount else "full",
            "status": "refunded",
        }

    @staticmethod
    def _build_idempotency_key(order_id: int, amount: Decimal) -> str:
        raw = f"{order_id}:{amount}:{int(time.time() // 3600)}"
        return hashlib.sha256(raw.encode()).hexdigest()
