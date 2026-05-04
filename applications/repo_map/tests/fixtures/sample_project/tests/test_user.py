"""Tests for User model and UserService."""
from ..models.user import User
from ..services.user_service import UserService
from ..config import Settings


def test_user_creation():
    user = User(name="Alice", email="alice@example.com")
    assert user.name == "Alice"
    assert user.is_active is True


def test_user_email_validation():
    valid = User(name="Bob", email="bob@test.com")
    invalid = User(name="Charlie", email="noatsign")
    assert valid.validate_email() is True
    assert invalid.validate_email() is False


def test_user_service_create():
    svc = UserService(Settings())
    user = svc.create("Dave", "dave@test.com")
    assert user.name == "Dave"


def test_user_service_get_by_email():
    svc = UserService(Settings())
    svc.create("Eve", "eve@test.com")
    found = svc.get_by_email("eve@test.com")
    assert found.name == "Eve"


def test_user_deactivate():
    user = User(name="Frank", email="frank@test.com")
    user.deactivate()
    assert user.is_active is False
