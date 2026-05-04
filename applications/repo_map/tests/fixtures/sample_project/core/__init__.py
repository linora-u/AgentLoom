"""Core infrastructure: database, exceptions, logging."""
from .database import Database
from .exceptions import AppError, NotFoundError, ValidationError

__all__ = ["Database", "AppError", "NotFoundError", "ValidationError"]
