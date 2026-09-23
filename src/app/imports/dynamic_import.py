"""
Dynamic module function loader.

Provides runtime dynamic loading of functions from modules.
"""

import importlib
import importlib.machinery
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any, Callable


_APPLICATION_IMPORT_LOCK = threading.RLock()


def _load_application_module(module: str):
    """Import project-owned tools without adding the project to sys.path.

    Python caches modules process-wide. If another project already owns the
    same applications namespace, reject the collision instead of executing its
    cached code under the new project's configuration.
    """
    from agentloom.config import C

    project_root = Path(C.agent_root).resolve()
    applications_root = project_root / "applications"
    with _APPLICATION_IMPORT_LOCK:
        package = sys.modules.get("applications")
        if package is None:
            spec = importlib.machinery.PathFinder.find_spec("applications", [str(project_root)])
            if spec is None:
                raise ModuleNotFoundError(f"No applications package in project '{project_root}'")
            package = importlib.util.module_from_spec(spec)
            sys.modules["applications"] = package
            try:
                if spec.loader is not None:
                    spec.loader.exec_module(package)
            except BaseException:
                sys.modules.pop("applications", None)
                raise
        package_paths = {Path(path).resolve() for path in getattr(package, "__path__", ())}
        if applications_root not in package_paths:
            raise ImportError(
                "The applications namespace is already loaded from another project; "
                "execute this project in a fresh interpreter."
            )
        loaded = importlib.import_module(module)
        origin = getattr(loaded, "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(applications_root):
            raise ImportError(f"Application tool '{module}' did not resolve inside '{applications_root}'")
        return loaded


def load_function(module: str, function: str) -> Callable[..., Any]:
    """
    Dynamically load a function by module path and function name.

    Args:
        module: Module path, supports:
            - Python module name: "agentloom.worker_agents.test_generation_agent"
        function: Function name, e.g. "generate_tests"

    Returns:
        Loaded function object.

    Raises:
        ImportError: Module import fails.
        AttributeError: Function does not exist.
        TypeError: Target is not callable.

    Example:
        >>> func = load_function("agentloom.worker_agents.test_generation_agent", "generate_tests")
        >>> result = func(args)
    """

    loaded_module = (
        _load_application_module(module)
        if module.startswith("applications.")
        else importlib.import_module(module)
    )

    # Get function.
    if not hasattr(loaded_module, function):
        raise AttributeError(
            f"Function '{function}' was not found in module '{module}'."
        )

    func = getattr(loaded_module, function)

    if not callable(func):
        raise TypeError(
            f"'{function}' is not callable, type: {type(func)}"
        )

    return func
