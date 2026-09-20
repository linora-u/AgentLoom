"""Compatibility imports; subprocess environment policy belongs to runtime."""
from agentloom.runtime.subprocess_env import (
    _INJECT as _INJECT,
    _SCRUB_EXACT as _SCRUB_EXACT,
    _SCRUB_PREFIXES as _SCRUB_PREFIXES,
    build_subprocess_env as build_subprocess_env,
)
