"""smolagents-owned terminal Tool contract."""
from typing import Any
from dataclasses import replace

from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.tool_gateway import ToolBinding
from agentloom.runtime.native_tools import ToolManifestEntry

def _return_final_answer(answer: Any) -> Any:
    return answer


def final_answer_binding() -> ToolBinding:
    """Return smolagents' explicit terminal Tool binding."""

    inputs_schema = {
        "answer": {
            "type": "string",
            "description": "The final answer to the problem",
            "required": True,
        }
    }
    binding = ToolBinding(
        definition=ToolDefinition(
            name="final_answer",
            description="Provides a final answer to the given problem.",
            parameters={
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The final answer to the problem",
                    }
                },
                "required": ["answer"],
            },
        ),
        forward=_return_final_answer,
        inputs_schema=inputs_schema,
        output_type="string",
    )
    return replace(binding, manifest_entry=ToolManifestEntry(
        logical_name="final_answer", visible_name="final_answer",
        owner="runtime", provider="smolagents", capability="completion.final",
        operation="control", parameters=binding.definition.parameters,
    ))
