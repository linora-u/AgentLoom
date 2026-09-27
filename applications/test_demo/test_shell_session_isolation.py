#!/usr/bin/env python3
"""Test shell session CWD isolation and ephemeral env semantics."""
import sys
import os
from pathlib import Path

# Add project root to Python path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from agentloom.app.factory import YamlAgentFactory, YamlConfiguredSupervisorAgent


def run_shell_session_isolation_test():
    """Test shell session isolation between supervisor and worker."""

    # 1. Load agent configuration.
    current_dir = Path(__file__).parent
    yaml_path = current_dir / "workflows" / "test_shell_session_isolation_supervisor.yaml"
    
    print(f"Loading config file: {yaml_path}")
    if not yaml_path.exists():
        print(f"Error: config file not found {yaml_path}")
        return

    config = YamlAgentFactory._load_config_from_file(yaml_path)
    
    # 2. Initialize agent.
    supervisor = YamlConfiguredSupervisorAgent(config=config)

    # The complete task is defined in the Supervisor YAML.
    print("\n" + "="*80)
    print("Starting shell session isolation agent task...")
    print("="*80 + "\n")
    
    try:
        result = supervisor.run()
        
        print("\n" + "="*80)
        print("Agent execution completed")
        print("="*80)
        print(f"Execution result:\n{result}")
        
    except Exception as e:
        print("\n" + "="*80)
        print("Execution failed!")
        print("="*80)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    
    run_shell_session_isolation_test()
