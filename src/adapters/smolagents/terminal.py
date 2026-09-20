"""smolagents terminal Tool contract (temporary old import remains supported)."""
from typing import Any

from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.tool_gateway import ToolBinding

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
    return ToolBinding(
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


