"""Stable failures exposed by Application Studio services."""

from __future__ import annotations


class StudioServiceError(RuntimeError):
    """A user-safe failure with a stable presentation code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
