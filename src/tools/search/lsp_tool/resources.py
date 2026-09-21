"""Lazy, invocation-owned language-server resources for optional LSP tools."""

from __future__ import annotations

import atexit
from threading import RLock
from typing import TYPE_CHECKING

from agentloom.runtime.resources import RuntimeKey

if TYPE_CHECKING:
    from agentloom.integrations.lsp import LSPServerManager

_lock = RLock()
_managers: dict[tuple[RuntimeKey, str], LSPServerManager] = {}


def get_lsp_manager() -> LSPServerManager:
    """Return a manager owned by the active Run and Agent instance.

    Direct callers outside an Application retain the explicit singleton API.
    Application tools initialize lazily and register concrete close callbacks.
    """
    from agentloom.integrations.lsp import LSPServerManager
    from agentloom.integrations.lsp.config import LSPConfig
    from agentloom.configuration import C
    from agentloom.runtime import get_current_run_context
    from agentloom.runtime.resources import register_resource
    from agentloom.runtime.trace import capture_explicit_execution_context

    context = get_current_run_context()
    if context is None:
        return LSPServerManager.get_instance()
    execution = capture_explicit_execution_context()
    owner = execution.agent_id
    if not owner:
        raise RuntimeError("LSP tools require a bound Agent instance")
    key = (context.runtime_key, owner)
    with _lock:
        existing = _managers.get(key)
        if existing is not None:
            return existing
        manager = LSPServerManager()

        def close() -> None:
            with _lock:
                if _managers.get(key) is manager:
                    del _managers[key]
            try:
                manager.shutdown()
            finally:
                atexit.unregister(manager.shutdown)

        try:
            config = execution.agent_config or {}
            manager.initialize(
                LSPConfig.from_yaml(config.get("lsp_servers", C.get("lsp_servers", {}))),
                project_root=str(C.agent_root),
            )
        except BaseException:
            close()
            raise
        _managers[key] = manager
        register_resource("optional:lsp", close, instance_id=owner)
        return manager
