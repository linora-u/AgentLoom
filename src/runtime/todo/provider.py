"""Compatibility alias for the smol-owned implementation."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("agentloom.adapters.smolagents.todo.provider")
