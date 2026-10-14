"""Application composition root for built-in Agent runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentloom.execution.agent_runtime import (
    AgentRuntime,
    RuntimeDefinition,
    RuntimeFactory,
    RuntimeRegistry,
)

if TYPE_CHECKING:
    from agentloom.schedules.mutations import ScheduleMutationService


def build_durable_attempt_coordinator(root, host, *, jobs=None):
    """One trusted default assembly for durable startup, stop and recovery."""
    from agentloom.execution.durable.coordinator import AttemptCoordinator, LegacyCoordinator
    if jobs is None:
        return AttemptCoordinator(root, host)
    return jobs if hasattr(jobs, "belongs_to") else LegacyCoordinator(jobs)


def build_schedule_mutations(
    project_root: str | Path,
) -> ScheduleMutationService:
    """Build Schedule mutations with Application Supervisor validation."""

    from agentloom.app.definition import (
        inspect_supervisor_definition,
    )
    from agentloom.schedules.mutations import ScheduleMutationService
    from agentloom.schedules.schema import ValidatedScheduleTarget

    root = Path(project_root).expanduser().resolve()

    def resolve_target(configured_path: str | Path) -> ValidatedScheduleTarget:
        try:
            inspection = inspect_supervisor_definition(
                root,
                str(configured_path),
            )
        except ValueError as exc:
            raise ValueError("yaml_path must identify a real, non-symlink project Supervisor Agent definition") from exc
        if inspection.errors:
            raise ValueError("yaml_path must identify a valid supervisor Agent definition")
        return ValidatedScheduleTarget(inspection.relative_path)

    return ScheduleMutationService(
        root,
        target_resolver=resolve_target,
    )


def build_builtin_runtime_registry(
    *,
    smolagents_factory: RuntimeFactory | None = None,
) -> RuntimeRegistry:
    """Build the production registry for AgentLoom's built-in runtimes."""

    smolagents_readiness = None
    if smolagents_factory is None:

        def build_smolagents(definition: RuntimeDefinition) -> AgentRuntime:
            from agentloom.runtimes.smolagents.runtime_factory import SmolagentsRuntimeFactory

            return SmolagentsRuntimeFactory()(definition)

        smolagents_factory = build_smolagents

        def smolagents_readiness() -> None:
            from importlib import import_module

            try:
                import_module("smolagents")
            except ImportError as error:
                raise RuntimeError(
                    "The smol runtime is not installed. Install 'AgentLoom[smol]' "
                    "or run uv sync --locked --extra smol in the checkout."
                ) from error

    registry = RuntimeRegistry()
    from agentloom.runtimes.smolagents.metadata import (
        CAPABILITIES as SMOLAGENTS_CAPABILITIES,
    )

    registry.register(
        "smolagents",
        capabilities=SMOLAGENTS_CAPABILITIES,
        factory=smolagents_factory,
        readiness=smolagents_readiness,
    )

    from agentloom.runtimes.pi.metadata import CAPABILITIES as PI_CAPABILITIES

    def pi_factory(definition: RuntimeDefinition) -> AgentRuntime:
        from agentloom.runtimes.pi.runtime import PiRuntime

        return PiRuntime(definition)

    def pi_readiness() -> None:
        from agentloom.runtimes.pi.install import installed_pi_entry

        installed_pi_entry()  # Read/probe only; installation remains an explicit CLI operation.

    registry.register("pi", capabilities=PI_CAPABILITIES, factory=pi_factory, readiness=pi_readiness)
    return registry
