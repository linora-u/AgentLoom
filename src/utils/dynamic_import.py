"""
Dynamic module function loader.

Provides runtime dynamic loading of functions from modules.
"""

import importlib
from typing import Any, Callable


def load_function(module: str, function: str) -> Callable[..., Any]:
    """
    Dynamically load a function by module path and function name.

    Args:
        module: Module path, supports:
            - Python module name: "src.worker_agents.test_generation_agent"
        function: Function name, e.g. "generate_tests"

    Returns:
        Loaded function object.

    Raises:
        ImportError: Module import fails.
        AttributeError: Function does not exist.
        TypeError: Target is not callable.

    Example:
        >>> func = load_function("src.worker_agents.test_generation_agent", "generate_tests")
        >>> result = func(args)
    """

    loaded_module = importlib.import_module(module)

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
