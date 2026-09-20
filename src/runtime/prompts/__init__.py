"""Common environment context, with lazy legacy smol template exports."""
from agentloom.runtime.prompts.environment import get_agent_environment_prompt

_LEGACY_EXPORTS = {
    "DEFAULT_TOOLCALLING_AGENT_PROMPT_PATH", "build_prompt_templates",
    "resolve_model_family_prompt_path", "resolve_prompt_path",
}
__all__ = ["get_agent_environment_prompt", *sorted(_LEGACY_EXPORTS)]


def __getattr__(name: str):
    if name in _LEGACY_EXPORTS:
        from agentloom.adapters.smolagents.prompts import prompt_builder
        return getattr(prompt_builder, name)
    raise AttributeError(name)
