"""Order business logic."""
from ..config import Settings
from ..core.database import Database
from ..core.exceptions import NotFoundError, ValidationError
from ..models.user import User
from ..models.order import Order, OrderStatus


class OrderService:
    """Handles order creation, pricing and lifecycle."""

    def __init__(self, settings: Settings):
        self.db = Database(url=settings.db_url)
        self.db.connect()

    def place(self, user: User, amount: float, discount_pct: float = 0.0) -> Order:
        if amount <= 0:
            raise ValidationError("amount", "must be positive")
        if discount_pct < 0 or discount_pct > 1:
            raise ValidationError("discount_pct", "must be between 0 and 1")
        order = Order(user=user, amount=amount, discount_pct=discount_pct)
        self.db.save("orders", f"{user.email}_{amount}", {
            "user_email": user.email,
            "amount": amount,
            "status": order.status.value,
        })
        return order

    def calculate_total(self, order: Order, tax_rate: float = 0.1) -> float:
        return order.total_with_tax(tax_rate)

    def confirm(self, order: Order) -> None:
        order.confirm()
        self._update_status(order)

    def cancel(self, order: Order) -> None:
        order.cancel()
        self._update_status(order)

    def _update_status(self, order: Order) -> None:
        key = f"{order.user.email}_{order.amount}"
        self.db.save("orders", key, {
            "user_email": order.user.email,
            "amount": order.amount,
            "status": order.status.value,
        })
