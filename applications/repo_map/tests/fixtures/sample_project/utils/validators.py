"""Common validation functions used across models and services."""

from __future__ import annotations

import re


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


def validate_email(email: str) -> str:
    """Validate and normalize an email address.

    Raises ValueError if the format is invalid.
    """
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email format: {email!r}")
    return email


def validate_phone(phone: str) -> str:
    """Validate a phone number (E.164-ish).

    Raises ValueError if the format is invalid.
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(phone):
        raise ValueError(f"Invalid phone format: {phone!r}")
    return phone
