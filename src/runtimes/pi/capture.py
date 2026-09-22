"""Validate a bridge-owned capture file before handing its data to public storage."""
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from agentloom.execution.native_tools import NativeResultCapture


def read_capture(directory: Path, authorization_id: str, digest: str, *, completed: bool) -> tuple[Any, NativeResultCapture]:
    if len(authorization_id) != 32 or any(c not in "0123456789abcdef" for c in authorization_id):
        raise ValueError("Invalid native capture authorization")
    path = directory / f"capture-{authorization_id}.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Native capture must be a regular file")
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Native capture digest mismatch")
    value = json.loads(data)
    if set(value) != {"raw_output", "result", "format", "display_truncated", "complete", "limitations"} or value["format"] not in {"text", "json"}:
        raise ValueError("Invalid native capture format")
    if value["format"] == "text" and not isinstance(value["raw_output"], str):
        raise ValueError("Invalid native text capture")
    if not isinstance(value["limitations"], list):
        raise ValueError("Invalid native capture limitations")
    capture = NativeResultCapture(value["raw_output"], complete=value["complete"],
        display_truncated=value["display_truncated"], limitations=tuple(value["limitations"]))
    if not completed:
        from dataclasses import replace
        capture = replace(capture, complete=False)
    return value["result"] if completed else None, capture
