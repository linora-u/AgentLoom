"""Matching helpers for Hook Plan entries."""

from __future__ import annotations

import re

from src.lib.logging import get_logger

logger = get_logger(__name__)
_SIMPLE_PATTERN = re.compile(r"^[a-zA-Z0-9_|]+$")


def matches_pattern(query: str, matcher: str | None) -> bool:
    if not matcher or matcher == "*":
        return True
    if _SIMPLE_PATTERN.fullmatch(matcher):
        return query in matcher.split("|") if "|" in matcher else query == matcher
    try:
        return bool(re.search(matcher, query))
    except re.error:
        logger.warning("Invalid Hook Handler matcher %r", matcher)
        return False
