"""Runtime-neutral per-turn output intent for Provider transport adapters."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class WireBudget:
    adapter: str
    cap: int | None
    source: str
    input_reserve: int = 0
    input_reserve_source: str = "unspecified"

    def evidence(self) -> dict:
        return {"effective_cap": self.cap, "wire_cap": self.cap, "source": self.source,
                "input_token_reserve": self.input_reserve,
                "input_reserve_source": self.input_reserve_source}


_WIRE_BUDGET: ContextVar[WireBudget | None] = ContextVar("agentloom_wire_budget", default=None)


def current_wire_budget() -> WireBudget | None:
    return _WIRE_BUDGET.get()


@contextmanager
def bind_wire_budget(policy: WireBudget) -> Iterator[None]:
    token = _WIRE_BUDGET.set(policy)
    try:
        yield
    finally:
        _WIRE_BUDGET.reset(token)
