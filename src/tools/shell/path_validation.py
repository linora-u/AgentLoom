"""Compatibility alias; policy is owned by the shared runtime.

See docs/tickets/agent-runtime-pi/04-implementation.md for removal criteria.
"""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("agentloom.runtime.tool_governance.shell.path_validation")
