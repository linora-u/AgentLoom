"""Custom exception hierarchy."""


class AppError(Exception):
    """Base application error."""
    def __init__(self, message: str, code: int = 500):
        super().__init__(message)
        self.code = code


class NotFoundError(AppError):
    """Resource not found."""
    def __init__(self, resource: str, key: str):
        super().__init__(f"{resource} not found: {key}", code=404)
        self.resource = resource
        self.key = key


class ValidationError(AppError):
    """Input validation failed."""
    def __init__(self, field: str, reason: str):
        super().__init__(f"Validation error on '{field}': {reason}", code=400)
        self.field = field
        self.reason = reason
