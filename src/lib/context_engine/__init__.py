"""Reversible context compression for AgentLoom runtime."""

from .config import ContextEngineConfig
from .engine import ContextEngine
from .models import CompressionResult, ContentKind, ContextEntry
from .runtime import (
    clear_current_context_engine,
    get_active_context_engine,
    get_current_context_engine,
    set_current_context_engine,
)

__all__ = [
    "CompressionResult",
    "ContentKind",
    "ContextEngine",
    "ContextEngineConfig",
    "ContextEntry",
    "clear_current_context_engine",
    "get_active_context_engine",
    "get_current_context_engine",
    "set_current_context_engine",
]
