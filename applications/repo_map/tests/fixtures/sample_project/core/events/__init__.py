"""Event bus — observer pattern for decoupled cross-module communication."""

from .bus import EventBus
from .decorators import on_event

__all__ = ["EventBus", "on_event"]
