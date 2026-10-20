"""Bind dynamic Trial storage without changing immutable candidate content."""

from copy import deepcopy

from .models import current_trial_context


def trial_config(base):
    """Use one Trial's runtime home for every root and nested Application Run."""
    context = current_trial_context()
    if context is None:
        return base
    from agentloom.config.config import UnifiedConfig

    raw = deepcopy(base.raw)
    raw.setdefault("runtime", {})["root_dir"] = str(context.runtime_root)
    # A programmatic binding deliberately has no disk-refresh markers: a
    # subsequent fresh invocation must preserve this dynamic storage context.
    return UnifiedConfig(raw, base.agent_root, base.llm.model_copy(deep=True))
