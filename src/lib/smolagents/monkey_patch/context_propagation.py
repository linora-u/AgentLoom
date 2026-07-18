"""Propagate AgentLoom ContextVars through smolagents' timeout executor."""

from __future__ import annotations

from contextvars import copy_context
from functools import wraps


def patch_local_python_executor_context() -> None:
    """Make LocalPythonExecutor's timeout thread inherit its caller context.

    ``smolagents.local_python_executor.evaluate_python_code`` decorates each
    execution with the module-level ``timeout`` function.  Upstream submits the
    callable directly to a fresh ThreadPoolExecutor, which drops ContextVars.
    Wrapping the callable with ``copy_context().run`` at submission time keeps
    the Hook Run, root/local run ids, task/subtask and agent metadata tied
    to the invocation that actually scheduled the code.
    """

    from smolagents import local_python_executor

    upstream_timeout = local_python_executor.timeout
    if getattr(upstream_timeout, "_agentloom_context_patched", False):
        return

    def context_propagating_timeout(timeout_seconds: int):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                context = copy_context()

                def execute_in_context():
                    return context.run(func, *args, **kwargs)

                return upstream_timeout(timeout_seconds)(execute_in_context)()

            return wrapper

        return decorator

    context_propagating_timeout._agentloom_context_patched = True  # type: ignore[attr-defined]
    local_python_executor.timeout = context_propagating_timeout
