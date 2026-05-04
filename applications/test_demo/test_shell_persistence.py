#!/usr/bin/env python3
"""
Test whether the shell tool persists state (like current working directory)
and isolates it properly between supervisor and worker agents.
"""
import sys
import os
from pathlib import Path

# Add project root to Python path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent
from src.trace import generate_id


def run_shell_persistence_test():
    """Test shell persistence and isolation between supervisor and worker."""

    # 1. Load agent configuration.
    current_dir = Path(__file__).parent
    yaml_path = current_dir / "workflows" / "test_shell_persist_supervisor.yaml"
    
    print(f"Loading config file: {yaml_path}")
    if not yaml_path.exists():
        print(f"Error: config file not found {yaml_path}")
        return

    config = YamlAgentFactory._load_config_from_file(yaml_path)
    
    # 2. Initialize agent.
    supervisor = YamlConfiguredSupervisorAgent(config=config)

    # 3. Build task.
    task_content = """
    请执行 shell 的隔离和持久性测试。
    
    步骤：
    1. 你（主 agent）执行 `shell_tool("cd /tmp")`
    2. 调度你的 worker (`shell_worker`)，让它执行: `shell_tool("cd /var/log")`
    3. 你再次执行：`shell_tool("pwd")` 记录你的当前目录
    4. 调度你的 worker (`shell_worker`) 执行: `shell_tool("pwd")` 获取它的当前目录
    5. 返回最终总结，主 agent 的 pwd 是什么，worker 的 pwd 是什么。以此证明你们各自的目录更改成功且互相不影响。
    """

    task_id = generate_id(task_content, prefix="task")

    print("\n" + "="*80)
    print("Starting shell persistence agent task...")
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
    
    run_shell_persistence_test()
