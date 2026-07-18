"""Lazy public exports for AgentLoom's smolagents integration.

Importing a lightweight submodule such as ``agent.agent_validation`` used to
execute this package initializer and eagerly import ``base_agent``.  That in
turn loads LiteLLM, every Agent tool, and provider integrations even for
read-only callers such as the TUI workspace index.  Keep the compatibility
surface, but resolve heavyweight symbols only when a caller actually asks for
them.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentLogger",
    "LogLevel",
    "Tool",
    "CodeAgent",
    "ToolCallingAgent",
    "CodeAgentV2",
    "ToolCallingAgentV2",
]

_UPSTREAM_EXPORTS = frozenset(
    {"AgentLogger", "LogLevel", "Tool", "CodeAgent", "ToolCallingAgent"}
)
_AGENTLOOM_EXPORTS = frozenset({"CodeAgentV2", "ToolCallingAgentV2"})


def __getattr__(name: str) -> Any:
    if name in _UPSTREAM_EXPORTS:
        import smolagents

        value = getattr(smolagents, name)
    elif name in _AGENTLOOM_EXPORTS:
        from .agent import base_agent

        value = getattr(base_agent, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
