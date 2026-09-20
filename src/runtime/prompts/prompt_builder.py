"""Legacy import for smol template assembly; environment context stays common."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("agentloom.adapters.smolagents.prompts.prompt_builder")
