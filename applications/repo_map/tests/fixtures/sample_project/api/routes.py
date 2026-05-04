"""API route definitions."""
from typing import Any

from ..services.user_service import UserService
from ..services.order_service import OrderService
from ..core.exceptions import AppError


class Router:
    """Simple router that maps endpoints to service calls."""

    def __init__(self, user_service: UserService, order_service: OrderService):
        self.user_svc = user_service
        self.order_svc = order_service

    def handle(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        try:
            if method == "POST" and path == "/users":
                user = self.user_svc.create(body["name"], body["email"])
                return {"status": 201, "data": {"name": user.name, "email": user.email}}

            if method == "GET" and path.startswith("/users/"):
                email = path.split("/users/")[1]
                user = self.user_svc.get_by_email(email)
                return {"status": 200, "data": {"name": user.name, "email": user.email}}

            if method == "POST" and path == "/orders":
                user = self.user_svc.get_by_email(body["user_email"])
                order = self.order_svc.place(user, body["amount"])
                total = self.order_svc.calculate_total(order)
                return {"status": 201, "data": {"total": total, "status": order.status.value}}

            return {"status": 404, "error": f"Not found: {method} {path}"}

        except AppError as e:
            return {"status": e.code, "error": str(e)}
