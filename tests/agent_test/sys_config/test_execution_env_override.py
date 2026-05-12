import pytest
from copy import deepcopy
from pathlib import Path
from src.lib.smolagents.agent.yaml_agent_factory import YamlConfiguredAgent
from src.lib.config.config import _ACTIVE_CONFIG, _load_merged_config
from src.lib.logging import initialize_global_logger_once

@pytest.fixture
def override_config(tmp_path, monkeypatch):
    from src.lib.config import config as config_module
    
    # Create mock directories
    agent_root = tmp_path / "AgentLoom"
    app_root = agent_root / "applications" / "test_app"
    workflows_dir = app_root / "workflows"
    config_dir = app_root / "config"
    system_config_dir = agent_root / "config"
    
    workflows_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    system_config_dir.mkdir(parents=True)
    
    # Create global configs
    (system_config_dir / "system.yaml").write_text("""
execution_env:
  type: local
  bash_path: /bin/bash
    """)
    (system_config_dir / "llm.yaml").write_text("""
model:
  default_model_type: powerful
  powerful:
    model: "openai/gpt-4o"
  summary:
    model: "openai/gpt-4o-mini"
""")
    
    # Create application config override
    (config_dir / "system.yaml").write_text("""
execution_env:
  type: docker
  bash_path: /bin/zsh
    """)
    
    agent_yaml_path = workflows_dir / "agent.yaml"
    agent_yaml_path.write_text("""
name: test_agent
workflow: |
  Step 1
    """)
    
    previous = config_module._ACTIVE_CONFIG
    try:
        # Load global
        config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=system_config_dir)
        yield agent_yaml_path
    finally:
        config_module._ACTIVE_CONFIG = previous

def test_execution_env_inherits_from_app_override(override_config):
    # This should pick up the app-level system.yaml via _yaml_file_path resolution
    agent_cfg = {
        "name": "test_agent",
        "description": "test",
        "workflow": "test",
        "_yaml_file_path": str(override_config)
    }
    initialize_global_logger_once("test_app")
    agent = YamlConfiguredAgent(config=agent_cfg, logger=None, model="dummy")
    
    normalized_env = agent._ensure_execution_normalized()
    
    # Because app config overrides system, and agent config does not override app config
    assert normalized_env.executor_type == "docker"
    # bash_path is silently ignored — shell is auto-detected from $SHELL

def test_execution_env_agent_config_overrides_app(override_config):
    agent_cfg = {
        "name": "test_agent",
        "description": "test",
        "workflow": "test",
        "_yaml_file_path": str(override_config),
        "execution_env": {
            "type": "wasm",
            "bash_path": "/usr/bin/fish"
        }
    }
    initialize_global_logger_once("test_app")
    agent = YamlConfiguredAgent(config=agent_cfg, logger=None, model="dummy")
    
    normalized_env = agent._ensure_execution_normalized()
    
    assert normalized_env.executor_type == "wasm"
    # bash_path is silently ignored — shell is auto-detected from $SHELL
