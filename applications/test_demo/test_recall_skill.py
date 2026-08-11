#!/usr/bin/env python3
"""Exercise catalogue discovery and on-demand Skill activation."""
import sys
import os
from pathlib import Path

# Add project root to Python path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent
from src.trace import generate_id
from src.lib.smolagents.memory.context_compression import ContextBudgetConfig


def run_recall_skill_test():
    """Test loading and execution of the agent-recall-with-files skill."""

    # 1. Load agent configuration.
    current_dir = Path(__file__).parent
    yaml_path = current_dir / "workflows" / "test_recall_agent.yaml"
    
    print(f"Loading config file: {yaml_path}")
    if not yaml_path.exists():
        print(f"Error: config file not found {yaml_path}")
        return

    # YamlAgentFactory discovers the Skill catalogue. The model activates one
    # body with skill(name); tools remain governed by the Agent configuration.
    config = YamlAgentFactory._load_config_from_file(yaml_path)
    
    # 2. Initialize agent.
    # Use SupervisorAgent because recall skill is typically used for complex tasks.
    supervisor = YamlConfiguredSupervisorAgent(config=config)

    # 3. Build task.
    # Goal: activate the Skill and then use the separately authorized file tools.
    task_content = """
    先调用 skill(name="agent-recall-with-files") 获取说明，再分析 'applications' 目录下所有的Python文件。
    请找出其中包含 'test' 字样的文件，读取它们的内容，提取出每个文件的文档字符串（docstring）说明。
    最后，在当前目录下创建一个名为 'test_summary.md' 的文件，将这些文件的路径和对应的功能说明按Markdown列表格式写入。
    
    这是一个多步骤的任务，请确保你的分析准确。
    """

    task_id = generate_id(task_content, prefix="task")

    print("\n" + "="*80)
    print("Starting agent task...")
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
    
    run_recall_skill_test()
