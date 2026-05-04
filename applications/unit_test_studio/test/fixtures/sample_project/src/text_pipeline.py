"""
Sample fixture module for Unit Test Studio smoke runs.

The functions are intentionally branch-heavy so generated tests have meaningful targets.
"""

from __future__ import annotations

import re
from collections import Counter


DEFAULT_STOP_WORDS = {"the", "a", "an", "and", "or", "to", "of"}


def normalize_user_message(
    text: str,
    *,
    strict: bool = False,
    max_len: int = 80,
    keep_punctuation: bool = False,
) -> str:
    """
    Normalize free-form user text into a stable comparison string.
    """
    if text is None:
        raise TypeError("text must not be None")
    if max_len <= 0:
        raise ValueError("max_len must be positive")

    cleaned = text.strip()
    if not cleaned:
        return "empty" if strict else ""

    if not keep_punctuation:
        cleaned = re.sub(r"[^\w\s]", " ", cleaned)

    cleaned = cleaned.lower()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if strict:
        tokens = [tok for tok in cleaned.split(" ") if tok and tok not in DEFAULT_STOP_WORDS]
        cleaned = " ".join(tokens)

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()

    return cleaned


def extract_keywords(message: str, limit: int = 5) -> list[str]:
    """
    Extract top keywords by frequency and length.
    """
    if limit <= 0:
        return []

    normalized = normalize_user_message(
        message,
        strict=True,
        max_len=200,
        keep_punctuation=False,
    )
    if not normalized:
        return []

    words = [w for w in normalized.split(" ") if len(w) > 2]
    if not words:
        return []

    counts = Counter(words)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [word for word, _ in ranked[:limit]]
