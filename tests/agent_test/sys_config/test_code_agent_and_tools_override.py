import pytest
from src.lib.smolagents.agent.yaml_agent_factory import YamlConfiguredAgent
from src.lib.config.config import _ACTIVE_CONFIG, _load_merged_config

@pytest.fixture
def code_agent_override_config(tmp_path, monkeypatch):
    from src.lib.config import config as config_module
    
    agent_root = tmp_path / "AgentLoom"
    app_root = agent_root / "applications" / "test_app"
    workflows_dir = app_root / "workflows"
    config_dir = app_root / "config"
    system_config_dir = agent_root / "config"
    
    workflows_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    system_config_dir.mkdir(parents=True)
    
    (system_config_dir / "system.yaml").write_text("""
code_agent:
  additional_functions: "*"
  additional_authorized_imports: "*"
default_loaded_tools:
  - shell_tool
    """)
    (system_config_dir / "llm.yaml").write_text("""
model:
  default_model_type: powerful
  powerful:
    model: "openai/gpt-4o"
  summary:
    model: "openai/gpt-4o-mini"
""")
    
    (config_dir / "system.yaml").write_text("""
code_agent:
  additional_functions: ["print"]
default_loaded_tools:
  - load_skill
    """)
    
    agent_yaml_path = workflows_dir / "agent.yaml"
    agent_yaml_path.write_text("""
name: test_agent
workflow: |
  Step 1
    """)
    
    previous = config_module._ACTIVE_CONFIG
    try:
        config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=system_config_dir)
        yield agent_yaml_path
    finally:
        config_module._ACTIVE_CONFIG = previous

def test_code_agent_and_tools_inherits_from_app_override(code_agent_override_config):
    from src.lib.logging import initialize_global_logger_once
    initialize_global_logger_once("test_app")
    
    agent_cfg = {
        "name": "test_agent",
        "description": "test",
        "workflow": "test",
        "_yaml_file_path": str(code_agent_override_config)
    }
    agent = YamlConfiguredAgent(config=agent_cfg, logger=None, model="dummy")
    
    # Check effective config
    code_cfg = agent._effective_agent_config.get("code_agent", {})
    assert code_cfg.get("additional_functions") == ["print"]
    
    default_tools = agent._effective_agent_config.get("default_loaded_tools", [])
    assert default_tools == ["load_skill"]
    
    # Also verify it got into runtime kwargs
    profile = agent._role_profile()
    kwargs = agent._build_execution_agent_kwargs(profile)
    assert "print" in kwargs["additional_functions"]
    assert callable(kwargs["additional_functions"]["print"])
    
    # Check get_tools returns correct default tools
    tools = agent._get_tools()
    tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in tools]
    assert "load_skill" in tool_names
    assert "shell_tool" not in tool_names
