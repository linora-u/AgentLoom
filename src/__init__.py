"""
AI Agents package.
"""

from src.encoding.terminal import configure_terminal_encoding

configure_terminal_encoding()

from src.lib.config import (
    C,
    get_code_agent_config,
    get_config,
    get_default_toolsets,
    get_model_config,
)
from src.runner import run_app

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "C",
    "get_config",
    "get_default_toolsets",
    "get_code_agent_config",
    "get_model_config",
    "run_app",
]
