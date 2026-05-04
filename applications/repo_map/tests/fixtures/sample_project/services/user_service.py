"""User business logic."""
from ..config import Settings
from ..core.database import Database
from ..core.exceptions import NotFoundError, ValidationError
from ..models.user import User


class UserService:
    """Handles user creation, lookup and validation."""

    def __init__(self, settings: Settings):
        self.db = Database(url=settings.db_url)
        self.db.connect()

    def create(self, name: str, email: str) -> User:
        if not name.strip():
            raise ValidationError("name", "cannot be empty")
        user = User(name=name, email=email)
        if not user.validate_email():
            raise ValidationError("email", f"invalid email: {email}")
        self.db.save("users", email, {"name": name, "email": email})
        return user

    def get_by_email(self, email: str) -> User:
        data = self.db.get("users", email)
        if not data:
            raise NotFoundError("User", email)
        return User(**data)

    def list_all(self) -> list[User]:
        return [User(**d) for d in self.db.list_all("users")]

    def deactivate(self, email: str) -> None:
        user = self.get_by_email(email)
        user.deactivate()
        self.db.save("users", email, {"name": user.name, "email": user.email, "is_active": False})
