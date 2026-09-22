"""Thin smolagents Tool declarations backed by AgentLoom's Tool Gateway."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolGateway
from smolagents import Tool


class SmolagentsToolGatewayProxy(Tool):
    """Expose one exact canonical definition without owning Tool execution."""

    skip_forward_signature_validation = True

    def __init__(
        self,
        *,
        gateway: ToolGateway,
        definition: ToolDefinition,
    ) -> None:
        parameters = dict(definition.parameters)
        properties = parameters.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(
                f"Tool {definition.name!r} parameters must contain object properties"
            )
        proxy_inputs = deepcopy(properties)
        for name, schema in proxy_inputs.items():
            if not isinstance(schema, dict):
                schema = proxy_inputs[name] = {}
            # smol requires local type metadata even for $ref/anyOf/boolean
            # schemas. The provider still receives the exact canonical schema
            # below, and the Gateway enforces it before tool execution.
            schema.setdefault("type", "any")
            schema.setdefault("description", "")
        self.name = definition.name
        self.description = definition.description
        self.inputs = proxy_inputs
        self.output_type = "any"
        self.is_initialized = True
        self._gateway = gateway
        self._agentloom_tool_definition = definition

    def forward(self, **kwargs: Any) -> Any:
        """Compatibility path for direct calls outside the provider Agent loop."""

        return self._gateway.invoke(
            call_id=uuid.uuid4().hex,
            tool_name=self.name,
            arguments=kwargs,
        ).direct_result()


def build_smolagents_tool_proxies(
    gateway: ToolGateway,
) -> list[SmolagentsToolGatewayProxy]:
    """Project an immutable Gateway definition snapshot to smolagents Tools."""

    definitions = gateway.definitions
    if not any(definition.name == "final_answer" for definition in definitions):
        raise ValueError(
            "Tool Gateway must explicitly provide the final_answer definition"
        )
    return [
        SmolagentsToolGatewayProxy(
            gateway=gateway,
            definition=definition,
        )
        for definition in definitions
    ]
