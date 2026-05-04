"""Order domain model."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .user import User


class OrderStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass
class Order:
    """Represents a customer order."""
    user: User
    amount: float
    status: OrderStatus = OrderStatus.PENDING
    discount_pct: float = 0.0

    def total_with_tax(self, tax_rate: float = 0.1) -> float:
        discounted = self.amount * (1 - self.discount_pct)
        return discounted * (1 + tax_rate)

    def confirm(self) -> None:
        if self.status != OrderStatus.PENDING:
            raise ValueError(f"Cannot confirm order in status: {self.status}")
        self.status = OrderStatus.CONFIRMED

    def cancel(self) -> None:
        if self.status == OrderStatus.SHIPPED:
            raise ValueError("Cannot cancel a shipped order")
        self.status = OrderStatus.CANCELLED
