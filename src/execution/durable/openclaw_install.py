"""Withdrawn package installer; OpenClaw is permitted as a source reference only.

Keep the historical entrypoints diagnostic until the source-informed Python
owner replaces their callers. None of these functions inspects assets, starts
Node/npm, downloads a package or selects a fallback owner.
"""
from __future__ import annotations

from pathlib import Path
from typing import NoReturn

DISABLED_REASON = "OpenClaw installation and execution are disabled; only source reference is permitted."


def _disabled() -> NoReturn:
    raise RuntimeError(DISABLED_REASON)


def managed_node() -> str:
    _disabled()


def asset_identity():
    _disabled()


def owner_runtime_root() -> Path:
    _disabled()


def installed_owner_runtime() -> Path:
    _disabled()


def install_openclaw() -> Path:
    _disabled()
