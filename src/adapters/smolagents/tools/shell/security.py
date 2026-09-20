"""Compatibility alias for the shared Shell policy."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("agentloom.runtime.tool_governance.shell.security")
