"""Entry point for sample project — a minimal e-commerce backend."""
from .config import Settings
from .services.user_service import UserService
from .services.order_service import OrderService


def run():
    settings = Settings()
    user_svc = UserService(settings)
    order_svc = OrderService(settings)

    user = user_svc.create("Alice", "alice@example.com")
    order = order_svc.place(user, amount=99.9)
    total = order_svc.calculate_total(order, tax_rate=0.08)

    print(f"User: {user.name}, Order total: ${total:.2f}")
    return total


if __name__ == "__main__":
    run()
