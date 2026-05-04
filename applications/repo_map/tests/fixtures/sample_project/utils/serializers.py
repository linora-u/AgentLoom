"""JSON serialization helpers with custom encoder for Decimal, datetime, etc."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class AppJSONEncoder(json.JSONEncoder):
    """Extended JSON encoder that handles common Python types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return super().default(obj)


def to_json(obj: Any, pretty: bool = False) -> str:
    """Serialize an object to JSON string."""
    return json.dumps(
        obj,
        cls=AppJSONEncoder,
        ensure_ascii=False,
        indent=2 if pretty else None,
    )


def from_json(raw: str) -> Any:
    """Deserialize a JSON string."""
    return json.loads(raw)
