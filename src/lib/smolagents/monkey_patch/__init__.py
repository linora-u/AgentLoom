"""AgentLoom runtime adapters for smolagents.

Only unavoidable upstream monkey patches are installed here. Tool-call parsing,
argument coercion, and rate limiting are owned by AgentLoom subclasses/wrappers
instead of mutating smolagents globals.
"""

from __future__ import annotations

from src.lib.smolagents.monkey_patch.context_propagation import (
    patch_local_python_executor_context,
)
from src.lib.smolagents.monkey_patch.memory_truncate import disable_smolagents_truncation
from src.lib.smolagents.monkey_patch.monitor_metrics import patch_monitor_metrics
from src.lib.smolagents.monkey_patch.reasoning_content_patch import patch_litellm_reasoning_content

_INSTALLED = False


def install_agentloom_runtime_adapters() -> None:
    """Install the small set of global adapters AgentLoom still requires."""

    global _INSTALLED
    if _INSTALLED:
        return

    disable_smolagents_truncation()
    patch_local_python_executor_context()
    patch_monitor_metrics()
    patch_litellm_reasoning_content()
    _INSTALLED = True
