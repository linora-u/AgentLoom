"""Static capabilities owned by the smolagents runtime."""

from agentloom.runtime.agent_runtime import RuntimeCapabilities

CAPABILITIES = RuntimeCapabilities(
    structured_tools=True,
    parallel_tools=True,
    checkpoint_resume=True,
    subagents=True,
    goal=True,
    stop_hooks=True,
)
