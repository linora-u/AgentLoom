"""Collision-resistant identifiers for isolated validation campaigns."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path


def default_campaign_id(prefix: str) -> str:
    """Return a human-readable ID that remains unique across rapid starts."""
    prefix = str(prefix or "").strip()
    if not prefix or Path(prefix).name != prefix:
        raise ValueError("campaign id prefix must be one non-empty path component")
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:12]}"


__all__ = ["default_campaign_id"]
