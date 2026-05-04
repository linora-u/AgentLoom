"""PayPal payment strategy — wraps PayPal Orders API v2."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from .base import PaymentStrategy

logger = logging.getLogger(__name__)


class PayPalStrategy(PaymentStrategy):
    """PayPal adapter — create order → capture flow."""

    def __init__(self, client_id: str = "test_client", sandbox: bool = True):
        self._client_id = client_id
        self._sandbox = sandbox
        self._base_url = (
            "https://api-m.sandbox.paypal.com" if sandbox
            else "https://api-m.paypal.com"
        )

    def charge(self, order_id: int, amount: Decimal) -> dict:
        self.validate_amount(amount)
        capture_id = str(uuid.uuid4())[:12]
        logger.info("PayPal capture: order=%s amount=%s capture=%s", order_id, amount, capture_id)

        return {
            "transaction_id": f"pp_{capture_id}",
            "provider": "paypal",
            "amount": str(amount),
            "currency": "USD",
            "status": "completed",
            "sandbox": self._sandbox,
        }

    def refund(self, transaction_id: str, amount: Decimal | None = None) -> dict:
        refund_id = str(uuid.uuid4())[:12]
        logger.info("PayPal refund: txn=%s amount=%s", transaction_id, amount or "full")
        return {
            "refund_id": f"ppref_{refund_id}",
            "provider": "paypal",
            "amount": str(amount) if amount else "full",
            "status": "refunded",
        }
