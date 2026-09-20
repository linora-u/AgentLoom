"""Legacy import of smol model-feedback recovery."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("agentloom.adapters.smolagents.error_recovery")
