#!/usr/bin/env python3
"""Test shell session CWD isolation and ephemeral env semantics."""
import sys
import os
from pathlib import Path

# Add project root to Python path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent
from src.trace import generate_id


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

    # 3. Build task.
    task_content = """
    请执行 shell 的 session 隔离和环境变量非持久化测试。
    
    步骤：
    1. 你（主 agent）执行 `shell_tool("cd /tmp")`
    2. 你执行 `shell_tool("export AGENTLOOM_SESSION_VAR=supervisor && echo $AGENTLOOM_SESSION_VAR")`
    3. 你再次执行 `shell_tool("echo $AGENTLOOM_SESSION_VAR")`，确认变量不跨调用保留
    4. 调度你的 worker (`shell_session_worker`) 执行 cd /var/log、inline export+echo、下一次 echo、pwd
    5. 返回最终总结，主 agent 的 pwd 是什么，worker 的 pwd 是什么，env 是否只在同命令内可见。
    """

    task_id = generate_id(task_content, prefix="task")

    print("\n" + "="*80)
    print("Starting shell session isolation agent task...")
    print("="*80 + "\n")
    
    try:
        result = supervisor.run(task_content, task_id=task_id)
        
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
