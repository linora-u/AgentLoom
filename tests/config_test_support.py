"""Credential-free configuration for unit tests that inject their own models."""
from pathlib import Path

from agentloom.config.config import UnifiedConfig
from agentloom.config.llm_config import LLMConfig
from agentloom.config.system_loader import load_project_system_config


def unit_config(root: Path) -> UnifiedConfig:
    source = Path(__file__).resolve().parents[1]
    raw = load_project_system_config(source)
    profile = {"model": "openai/unit-test", "adapter": "openai_chat", "api_key": "unit-test"}
    models = LLMConfig.from_dict({"model": {"default_model_type": "powerful",
        **{name: profile for name in ("powerful", "summary", "test")}}})
    return UnifiedConfig(raw, agent_root=root, llm_config=models)
