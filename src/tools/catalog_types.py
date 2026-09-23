"""Metadata shared by independently owned tool catalog partitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolImplementation:
    """Import reference for a tool implementation without importing it."""

    module: str
    attribute: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    implementation: ToolImplementation
    toolset: str
    description: str
    category: str
    is_read_only: bool
    is_destructive: bool
    is_concurrency_safe: bool
    max_result_chars: int | None
    fixed_arg_names: tuple[str, ...]
    accepts_extra_fixed_args: bool = False
    path_params: tuple[str, ...] = ()
    output_kind: str = "text"
    owner: Literal["runtime", "platform"] = "platform"
    provider: str = "agentloom"
    capability: str = ""
    operation: Literal["read", "write", "shell", "platform", "control"] = "control"
    logical_name: str | None = None
    command_parameter: str | None = None


def _spec(
    name: str,
    implementation_module: str,
    toolset: str,
    description: str,
    category: str,
    *,
    fixed_arg_names: Iterable[str],
    accepts_extra_fixed_args: bool = False,
    is_read_only: bool,
    is_destructive: bool = False,
    is_concurrency_safe: bool = True,
    max_result_chars: int | None = 20000,
    path_params: Iterable[str] = (),
    output_kind: str = "text",
    owner: Literal["runtime", "platform"],
    provider: str,
    capability: str,
    operation: Literal["read", "write", "shell", "platform", "control"],
    logical_name: str | None = None,
    command_parameter: str | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        logical_name=logical_name,
        owner=owner,
        provider=provider,
        capability=capability,
        operation=operation,
        command_parameter=command_parameter,
        implementation=ToolImplementation(
            module=implementation_module,
            attribute=name,
        ),
        toolset=toolset,
        description=description,
        category=category,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        is_concurrency_safe=is_concurrency_safe,
        max_result_chars=max_result_chars,
        fixed_arg_names=tuple(fixed_arg_names),
        accepts_extra_fixed_args=accepts_extra_fixed_args,
        path_params=tuple(path_params),
        output_kind=output_kind,
    )
