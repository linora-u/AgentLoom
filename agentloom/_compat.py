"""Lazy compatibility for the historical source layout.

Leaf imports return the implementation module itself, not a copied namespace:
monkeypatches, registries, config proxies and ContextVars remain shared. Empty
historical grouping packages are synthesized only where the old and new child
names differ. They never contain another implementation.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from importlib import import_module
from types import ModuleType

# Populated from the responsibility move table, then kept as public compatibility.
_MODULE_MOVES = {
    "src.runner": "agentloom.application.runner",
    "src.application_run": "agentloom.application.run",
    "src.application_run_lifecycle": "agentloom.application.lifecycle",
    "src.application_revision": "agentloom.application.revision",
    "src.workflows.workflow_manager": "agentloom.application.workflows",
    "src.lib.config": "agentloom.configuration",
    "src.lib.runtime": "agentloom.runtime",
    "src.lib.checkpoint": "agentloom.runtime.checkpoint",
    "src.lib.todo": "agentloom.runtime.todo",
    "src.lib.goal": "agentloom.runtime.goal",
    "src.lib.heartbeat": "agentloom.runtime.heartbeat",
    "src.lib.context_engine": "agentloom.runtime.context_engine",
    "src.lib.permissions": "agentloom.runtime.permissions",
    "src.lib.logging": "agentloom.runtime.logging",
    "src.lib.concurrency": "agentloom.runtime.concurrency",
    "src.lib.trusted_memory_evidence": "agentloom.runtime.trusted_memory_evidence",
    "src.lib.utils.workspace": "agentloom.runtime.workspace",
    "src.lib.utils.dynamic_import": "agentloom.utils.dynamic_import",
    "src.trace": "agentloom.runtime.trace",
    "src.extensions.self_learning": "agentloom.self_learning",
    "src.mcp": "agentloom.adapters.mcp",
    "src.services.lsp": "agentloom.adapters.lsp",
    "src.lib.smolagents.agent.agent_validation": "agentloom.application.validation",
    "src.lib.smolagents.agent.runtime_validation": "agentloom.application.readiness",
    "src.lib.smolagents.agent.base_agent": "agentloom.runtime.agent",
    "src.lib.smolagents.agent.invocation": "agentloom.runtime.invocation",
    "src.lib.smolagents.agent.yaml_agent_factory": "agentloom.runtime.factory",
    "src.lib.smolagents.agent.loom_mixin": "agentloom.runtime.loom_mixin",
    "src.lib.smolagents.agent.agent_env": "agentloom.runtime.prompts.environment",
    "src.lib.smolagents.agent.tool_argument_coercion": "agentloom.adapters.smolagents.tool_argument_coercion",
    "src.lib.smolagents.hooks.tool_shim": "agentloom.adapters.smolagents.tool_shim",
    "src.lib.smolagents.hooks": "agentloom.runtime.hooks",
    "src.lib.smolagents.skills": "agentloom.runtime.skills",
    "src.lib.smolagents.prompts": "agentloom.runtime.prompts",
    "src.lib.smolagents.memory": "agentloom.runtime.memory",
    "src.lib.smolagents.error_recovery": "agentloom.runtime.error_recovery",
    "src.lib.smolagents.tool_protocol": "agentloom.adapters.smolagents.tool_protocol",
    "src.lib.smolagents.tools": "agentloom.adapters.smolagents.tools",
    "src.lib.smolagents.models": "agentloom.adapters.smolagents.models",
    "src.lib.smolagents.monkey_patch": "agentloom.adapters.smolagents.monkey_patch",
}
_NAMESPACE_EXPORTS = {
    "src.lib": None,
    "src.lib.smolagents": "agentloom.adapters.smolagents",
    "src.lib.smolagents.agent": None,
    "src.lib.smolagents.hooks": "agentloom.runtime.hooks",
    "src.lib.utils": None,
    "src.extensions": None,
    "src.services": None,
    "src.workflows": None,
}


def _target(name: str) -> str | None:
    for old in sorted(_MODULE_MOVES, key=len, reverse=True):
        if name == old or name.startswith(old + "."):
            return _MODULE_MOVES[old] + name[len(old) :]
    if name.startswith("src."):
        return "agentloom" + name[3:]
    return None


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self.target = target
        self._metadata: dict[str, object] = {}

    def create_module(self, spec):
        module = import_module(self.target)
        # importlib overwrites __spec__ even for an existing module returned by
        # create_module. Restore its true loader/package for relative imports,
        # resources, inspect and subsequent canonical reloads.
        self._metadata = {key: getattr(module, key) for key in ("__name__", "__package__", "__loader__", "__spec__")}
        return module

    def exec_module(self, module: ModuleType) -> None:
        module.__dict__.update(self._metadata)

    def get_code(self, fullname: str):
        # runpy uses get_code rather than create/exec_module for python -m.
        spec = importlib.util.find_spec(self.target)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot locate compatibility target {self.target}")
        return spec.loader.get_code(self.target)

    def get_source(self, fullname: str):
        spec = importlib.util.find_spec(self.target)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot locate compatibility target {self.target}")
        return spec.loader.get_source(self.target)


class _NamespaceLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module: ModuleType) -> None:
        target = _NAMESPACE_EXPORTS[module.__name__]
        module.__doc__ = "Compatibility namespace; implementations live in their responsibility owners."
        if target is None:
            return

        def get_attribute(name: str):
            if name.startswith("__") and name not in {"__all__"}:
                raise AttributeError(name)
            return getattr(import_module(target), name)

        module.__getattr__ = get_attribute
        module.__dir__ = lambda: sorted(set(module.__dict__) | set(dir(import_module(target))))


class _LegacyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname in _NAMESPACE_EXPORTS:
            return importlib.util.spec_from_loader(fullname, _NamespaceLoader(), is_package=True)
        canonical = _target(fullname)
        if canonical is None:
            return None
        try:
            spec = importlib.util.find_spec(canonical)
        except ModuleNotFoundError:
            return None
        if spec is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(canonical),
            origin=spec.origin,
            is_package=spec.submodule_search_locations is not None,
        )


def install_legacy_imports() -> None:
    if not any(isinstance(finder, _LegacyFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _LegacyFinder())
