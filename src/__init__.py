"""Compatibility access to AgentLoom's canonical package.

Implementation modules are lazy aliases installed by :mod:`agentloom._compat`.
This historical root stays separate so renamed grouping packages remain valid.
"""

import agentloom as _canonical

__version__ = _canonical.__version__
__all__ = _canonical.__all__


def __getattr__(name: str):
    if name in __all__:
        return getattr(_canonical, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
