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
    "src.runner": "src.application.runner",
    "src.application_run": "src.application.run",
    "src.application_run_lifecycle": "src.application.lifecycle",
    "src.application_revision": "src.application.revision",
    "src.workflows.workflow_manager": "src.application.workflows",
    "src.lib.config": "src.configuration",
    "src.lib.runtime": "src.runtime",
    "src.lib.checkpoint": "src.runtime.checkpoint",
    "src.lib.todo": "src.runtime.todo",
    "src.lib.goal": "src.runtime.goal",
    "src.lib.heartbeat": "src.runtime.heartbeat",
    "src.lib.context_engine": "src.runtime.context_engine",
    "src.lib.permissions": "src.runtime.permissions",
    "src.lib.logging": "src.runtime.logging",
    "src.lib.concurrency": "src.runtime.concurrency",
    "src.lib.trusted_memory_evidence": "src.runtime.trusted_memory_evidence",
    "src.lib.utils.workspace": "src.runtime.workspace",
    "src.lib.utils.dynamic_import": "src.utils.dynamic_import",
    "src.trace": "src.runtime.trace",
    "src.extensions.self_learning": "src.self_learning",
    "src.mcp": "src.adapters.mcp",
    "src.services.lsp": "src.adapters.lsp",
    "src.lib.smolagents.agent.agent_validation": "src.application.validation",
    "src.lib.smolagents.agent.runtime_validation": "src.application.readiness",
    "src.lib.smolagents.agent.base_agent": "src.runtime.agent",
    "src.lib.smolagents.agent.invocation": "src.runtime.invocation",
    "src.lib.smolagents.agent.yaml_agent_factory": "src.runtime.factory",
    "src.lib.smolagents.agent.loom_mixin": "src.runtime.loom_mixin",
    "src.lib.smolagents.agent.agent_env": "src.runtime.prompts.environment",
    "src.lib.smolagents.agent.tool_argument_coercion": "src.adapters.smolagents.tool_argument_coercion",
    "src.lib.smolagents.hooks.tool_shim": "src.adapters.smolagents.tool_shim",
    "src.lib.smolagents.hooks": "src.runtime.hooks",
    "src.lib.smolagents.skills": "src.runtime.skills",
    "src.lib.smolagents.prompts": "src.runtime.prompts",
    "src.lib.smolagents.memory": "src.runtime.memory",
    "src.lib.smolagents.error_recovery": "src.runtime.error_recovery",
    "src.lib.smolagents.tool_protocol": "src.adapters.smolagents.tool_protocol",
    "src.lib.smolagents.tools": "src.adapters.smolagents.tools",
    "src.lib.smolagents.models": "src.adapters.smolagents.models",
    "src.lib.smolagents.monkey_patch": "src.adapters.smolagents.monkey_patch",
}
_NAMESPACE_EXPORTS = {
    "src.lib": None,
    "src.lib.smolagents": "src.adapters.smolagents",
    "src.lib.smolagents.agent": None,
    "src.lib.smolagents.hooks": "src.runtime.hooks",
    "src.lib.utils": None,
    "src.extensions": None,
    "src.services": None,
    "src.workflows": None,
}


def _target(name: str) -> str | None:
    for old in sorted(_MODULE_MOVES, key=len, reverse=True):
        if name == old or name.startswith(old + "."):
            return _MODULE_MOVES[old] + name[len(old) :]
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
