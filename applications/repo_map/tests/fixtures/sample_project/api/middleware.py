"""Request middleware — authentication and logging."""
from typing import Callable, Any


class AuthMiddleware:
    """Simple token-based authentication middleware."""

    def __init__(self, secret: str = "dev-secret"):
        self.secret = secret

    def authenticate(self, token: str | None) -> bool:
        if not token:
            return False
        return token == self.secret

    def wrap(self, handler: Callable, token: str | None) -> dict[str, Any]:
        if not self.authenticate(token):
            return {"status": 401, "error": "Unauthorized"}
        return handler()


class LoggingMiddleware:
    """Logs request/response for debugging."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.log: list[str] = []

    def before_request(self, method: str, path: str) -> None:
        if self.enabled:
            self.log.append(f">> {method} {path}")

    def after_response(self, status: int) -> None:
        if self.enabled:
            self.log.append(f"<< {status}")

    def get_log(self) -> list[str]:
        return list(self.log)
