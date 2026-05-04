"""Tests for Order model and OrderService."""
from ..models.user import User
from ..models.order import Order, OrderStatus
from ..services.order_service import OrderService
from ..config import Settings


def test_order_creation():
    user = User(name="Alice", email="alice@test.com")
    order = Order(user=user, amount=100.0)
    assert order.status == OrderStatus.PENDING
    assert order.amount == 100.0


def test_order_total_with_tax():
    user = User(name="Bob", email="bob@test.com")
    order = Order(user=user, amount=100.0, discount_pct=0.1)
    total = order.total_with_tax(tax_rate=0.08)
    assert abs(total - 97.2) < 0.01  # 100 * 0.9 * 1.08


def test_order_confirm():
    user = User(name="Charlie", email="charlie@test.com")
    order = Order(user=user, amount=50.0)
    order.confirm()
    assert order.status == OrderStatus.CONFIRMED


def test_order_cancel():
    user = User(name="Dave", email="dave@test.com")
    order = Order(user=user, amount=75.0)
    order.cancel()
    assert order.status == OrderStatus.CANCELLED


def test_order_service_place():
    svc = OrderService(Settings())
    user = User(name="Eve", email="eve@test.com")
    order = svc.place(user, 200.0)
    assert order.amount == 200.0
    assert order.status == OrderStatus.PENDING
