"""User domain model."""
from dataclasses import dataclass


@dataclass
class User:
    """Represents a registered user."""
    name: str
    email: str
    is_active: bool = True

    def greet(self) -> str:
        return f"Hello, {self.name}!"

    def validate_email(self) -> bool:
        return "@" in self.email and "." in self.email.split("@")[-1]

    def deactivate(self) -> None:
        self.is_active = False
