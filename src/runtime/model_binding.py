"""A resolved, runtime-neutral binding for canonical model turns."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agentloom.configuration.model_adapters import AdapterKind
from agentloom.runtime.model_protocol import (
    ModelItem,
    ModelTurnAdapter,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)


def _frozen_options(options: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        normalized = json.loads(json.dumps(dict(options), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("model binding options must be JSON serializable") from exc
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class ModelTurnBinding:
    """One resolved model profile bound to its canonical turn adapter.

    Agent runtimes consume this value without knowing how the model profile
    was loaded or which provider SDK implements the selected wire protocol.
    Runtime adapters may project their native message types onto ``turn`` but
    must not reinterpret the resolved profile.
    """

    model_type: str
    model_id: str
    adapter: ModelTurnAdapter = field(repr=False, compare=False)
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)
    max_tokens: int = 0
    context_window: int = 0
    max_output_tokens: int = 0
    input_token_limit: int = 0
    requests_per_minute: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        normalized_type = self.model_type.strip().lower()
        if not normalized_type:
            raise ValueError("model_type must be non-empty")
        if not self.model_id:
            raise ValueError("model_id must be non-empty")
        for field_name in (
            "max_tokens",
            "context_window",
            "max_output_tokens",
            "input_token_limit",
            "requests_per_minute",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.context_window and self.max_output_tokens > self.context_window:
            raise ValueError("max_output_tokens cannot exceed context_window")
        object.__setattr__(self, "model_type", normalized_type)
        object.__setattr__(self, "options", _frozen_options(self.options))

    @property
    def adapter_id(self) -> AdapterKind:
        """Return the explicitly selected wire protocol."""

        return self.adapter.adapter_id

    def turn(
        self,
        *,
        items: tuple[ModelItem, ...],
        tools: tuple[ToolDefinition, ...] = (),
        instructions: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ModelTurnResult:
        """Execute one canonical model turn with optional per-turn overrides."""

        merged_options = dict(self.options)
        if options:
            merged_options.update(options)
        return self.adapter.turn(
            ModelTurnRequest(
                model=self.model_id,
                items=items,
                tools=tools,
                instructions=instructions,
                options=merged_options,
            )
        )
