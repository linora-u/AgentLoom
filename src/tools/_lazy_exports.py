"""Shared support for compatibility exports that must not import sibling tools."""

import sys
from collections.abc import Mapping, MutableMapping
from importlib import import_module
from types import ModuleType
from typing import Any

LazyExportMap = Mapping[str, tuple[str, str]]
_MISSING = object()


class _LazyExportsModule(ModuleType):
    """Keep function exports stable when Python installs a same-named submodule."""

    def __getattribute__(self, name: str) -> Any:
        namespace = ModuleType.__getattribute__(self, "__dict__")
        exports = namespace.get("_EXPORTS", {})
        if name in exports:
            current = namespace.get(name, _MISSING)
            if current is _MISSING or isinstance(current, ModuleType):
                package_name = ModuleType.__getattribute__(self, "__name__")
                return resolve_lazy_export(package_name, namespace, exports, name)
        return ModuleType.__getattribute__(self, name)


def resolve_lazy_export(
    package_name: str,
    namespace: MutableMapping[str, Any],
    exports: LazyExportMap,
    name: str,
) -> Any:
    """Resolve and cache one declared package export."""

    try:
        module_name, attribute = exports[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, package_name), attribute)
    namespace[name] = value
    return value


def install_lazy_exports(package_name: str) -> None:
    """Install collision-safe lookup on one implementation-group package."""

    module = sys.modules[package_name]
    if not isinstance(module, _LazyExportsModule):
        module.__class__ = _LazyExportsModule
