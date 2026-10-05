"""Bridge capabilities and AgentLoom-only configuration boundaries."""
from collections.abc import Mapping

from agentloom.execution.agent_runtime import RuntimeCapabilities, RuntimeModelSelection

SDK_VERSION = "1.0.0"
BRIDGE_VERSION = 1
CAPABILITIES = RuntimeCapabilities(
    True,
    True,
    True,
    True,
    goal=True,
    stop_hooks=True,
    structured_output=True,
)


def validate_options(options: Mapping) -> None:
    # The Stop gate belongs to AgentLoom; all SDK settings pass through unchanged.
    value = options.get("max_stop_attempts", 3)
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("pi runtime_options.max_stop_attempts must be an integer from 1 to 100")


def validate_model(model: RuntimeModelSelection) -> None:
    if model.protocol not in {"openai_chat", "openai_responses", "openai_codex_responses",
                              "openai_chatgpt_responses"}:
        raise ValueError(f"Pi bridge does not map model protocol {model.protocol!r}")
