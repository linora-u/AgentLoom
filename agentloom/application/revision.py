"""Deterministic content revisions for one AgentLoom Application."""

from __future__ import annotations

import hashlib
from pathlib import Path

MAX_REVISION_FILES = 4096
MAX_REVISION_BYTES = 64 * 1024 * 1024


def application_revision(application_root: Path) -> str:
    root = application_root.resolve(strict=True)
    if application_root.is_symlink() or not root.is_dir():
        raise ValueError("Application root must be a real directory")
    digest = hashlib.sha256()
    total = 0
    count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file():
            continue
        count += 1
        if count > MAX_REVISION_FILES:
            raise ValueError("Application contains too many files for a bounded revision")
        payload = path.read_bytes()
        total += len(payload)
        if total > MAX_REVISION_BYTES:
            raise ValueError("Application is too large for a bounded revision")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


__all__ = ["application_revision"]
