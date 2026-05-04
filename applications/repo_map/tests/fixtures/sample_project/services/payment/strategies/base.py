"""Abstract payment strategy — all providers implement this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal


class PaymentStrategy(ABC):
    """Interface for payment provider adapters.

    Each concrete strategy encapsulates provider-specific logic:
    API credentials, request formats, idempotency handling, etc.
    """

    @abstractmethod
    def charge(self, order_id: int, amount: Decimal) -> dict:
        """Execute a charge and return a receipt dict.

        Returns:
            dict with keys: transaction_id, provider, amount, currency, status
        """

    @abstractmethod
    def refund(self, transaction_id: str, amount: Decimal | None = None) -> dict:
        """Refund a previous charge (full or partial).

        Args:
            transaction_id: The original transaction to refund.
            amount: If None, full refund. Otherwise partial.
        """

    def validate_amount(self, amount: Decimal) -> None:
        """Guard: reject zero or negative amounts."""
        if amount <= 0:
            raise ValueError(f"Amount must be positive, got {amount}")
