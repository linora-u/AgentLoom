"""smolagents-owned terminal Tool contract."""

from dataclasses import replace
from typing import Any

from agentloom.execution.agent_runtime import OutputContract, copy_json_value
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.native_tools import ToolManifestEntry
from agentloom.execution.schema_validation import rebase_local_schema_references
from agentloom.execution.tool_gateway import ToolBinding


class _OutputValidationError(ValueError):
    """A final_answer call that does not satisfy the Agent output contract."""

    kind = "output_validation"
    stage = "output_validation"


def _return_final_answer(answer: Any) -> Any:
    return answer


def final_answer_binding(
    output_contract: OutputContract | None = None,
) -> ToolBinding:
    """Return smolagents' explicit terminal Tool binding."""

    if output_contract is not None:
        output_schema = copy_json_value(
            output_contract.schema,
            field_name="output contract schema",
        )
        assert isinstance(output_schema, dict)
        answer_schema = rebase_local_schema_references(
            output_schema,
            pointer="#/properties/answer",
        )
    else:
        answer_schema = {
            "type": "string",
            "description": "The final answer to the problem",
        }
    inputs_schema = {
        "answer": {
            **answer_schema,
            "required": True,
        }
    }
    input_validator = None
    if output_contract is not None:

        def validate_terminal(arguments: dict[str, Any]) -> None:
            try:
                if set(arguments) != {"answer"}:
                    raise ValueError(
                        "final_answer requires exactly one 'answer' field"
                    )
                output_contract.validate(arguments["answer"])
            except ValueError as exc:
                raise _OutputValidationError(str(exc)) from exc

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
