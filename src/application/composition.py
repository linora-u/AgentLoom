"""Application composition root for built-in Agent runtimes."""
from __future__ import annotations

from agentloom.runtime.agent_runtime import (
    AgentRuntime,
    RuntimeDefinition,
    RuntimeFactory,
    RuntimeRegistry,
    SMOLAGENTS_CAPABILITIES,
)


def build_builtin_runtime_registry(
    *,
    smolagents_factory: RuntimeFactory | None = None,
) -> RuntimeRegistry:
    """Build the production registry for AgentLoom's built-in runtimes."""

    if smolagents_factory is None:
        def build_smolagents(definition: RuntimeDefinition) -> AgentRuntime:
            from agentloom.runtimes.smolagents.runtime_factory import SmolagentsRuntimeFactory

            return SmolagentsRuntimeFactory()(definition)

        smolagents_factory = build_smolagents

    registry = RuntimeRegistry()
    registry.register(
        "smolagents",
        capabilities=SMOLAGENTS_CAPABILITIES,
        factory=smolagents_factory,
    )

    from agentloom.runtimes.pi.metadata import CAPABILITIES as PI_CAPABILITIES

    def pi_factory(definition: RuntimeDefinition) -> AgentRuntime:
        from agentloom.runtimes.pi.runtime import PiRuntime

        return PiRuntime(definition)

    registry.register("pi", capabilities=PI_CAPABILITIES, factory=pi_factory)
    return registry
