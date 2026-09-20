"""Compatibility alias; implementation is owned by the smol Agent.

See docs/tickets/agent-runtime-pi/04-implementation.md for removal criteria.
"""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("agentloom.adapters.smolagents.tools.shell.security")
