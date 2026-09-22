"""smolagents-owned terminal Tool contract."""

from copy import deepcopy
from dataclasses import replace
from typing import Any

from agentloom.execution.agent_runtime import OutputContract
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.native_tools import ToolManifestEntry
from agentloom.execution.tool_gateway import ToolBinding


def _return_final_answer(answer: Any) -> Any:
    return answer


def _embed_output_schema(
    value: Any,
    *,
    pointer: str = "#/properties/answer",
) -> Any:
    """Embed one self-contained schema without changing local pointer meaning."""

    if isinstance(value, dict):
        embedded = {}
        for key, child in value.items():
            if (
                key in {"$ref", "$dynamicRef"}
                and isinstance(child, str)
                and child.startswith("#/")
            ):
                embedded[key] = pointer + child[1:]
            elif key in {"$ref", "$dynamicRef"} and child == "#":
                embedded[key] = pointer
            else:
                embedded[key] = _embed_output_schema(
                    child,
                    pointer=pointer,
                )
        return embedded
    if isinstance(value, list):
        return [
            _embed_output_schema(child, pointer=pointer)
            for child in value
        ]
    return deepcopy(value)


def final_answer_binding(
    output_contract: OutputContract | None = None,
) -> ToolBinding:
    """Return smolagents' explicit terminal Tool binding."""

    answer_schema = (
        _embed_output_schema(dict(output_contract.schema))
        if output_contract is not None
        else {
            "type": "string",
            "description": "The final answer to the problem",
        }
    )
    inputs_schema = {
        "answer": {
            **answer_schema,
            "required": True,
        }
    }
    input_validator = None
    if output_contract is not None:

        def validate_terminal(arguments: dict[str, Any]) -> None:
            if set(arguments) != {"answer"}:
                raise ValueError(
                    "final_answer requires exactly one 'answer' field"
                )
            output_contract.validate(arguments["answer"])

        input_validator = validate_terminal

    binding = ToolBinding(
        definition=ToolDefinition(
            name="final_answer",
            description="Provides a final answer to the given problem.",
            parameters={
                "type": "object",
                "properties": {
                    "answer": answer_schema,
                },
                "required": ["answer"],
                "additionalProperties": False,
            },
            strict=True,
        ),
        forward=_return_final_answer,
        inputs_schema=inputs_schema,
        output_type=(
            str(answer_schema.get("type", "any"))
            if isinstance(answer_schema, dict)
            else "any"
        ),
        input_validator=input_validator,
    )
    return replace(binding, manifest_entry=ToolManifestEntry(
        logical_name="final_answer", visible_name="final_answer",
        owner="runtime", provider="smolagents", capability="completion.final",
        operation="control", parameters=binding.definition.parameters,
    ))
