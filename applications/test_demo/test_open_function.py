#!/usr/bin/env python3
"""
Test whether the `open` function can be used correctly in agent-executed code.
"""
import sys
import os

# Add project root to Python path.
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from agentloom.app.runner import run_app

def test_open_function():
    """Test whether the agent can use the `open` function."""
    
    # Set up logging.
    
    # Create temporary test file.
    test_file_path = "/private/tmp/agentloom_test_open.txt"
    test_content = "这是测试内容，用于验证 agent 可以使用 open 函数"
    
    # Create test file first.
    with open(test_file_path, 'w', encoding='utf-8') as f:
        f.write(test_content)
    
    print(f"✓ 测试文件已创建: {test_file_path}")
    
    # Create agent.
    agent_config_path = os.path.join(
        project_root, 
        'applications/test_demo/workflows/test_open_agent.yaml'
    )
    
    # The task is defined in the Agent YAML.
    print("\n" + "="*80)
    print("开始测试任务...")
    print("="*80 + "\n")
    
    try:
        result = run_app(agent_config_path)
        print("\n" + "="*80)
        print("测试成功！Agent 能够正常使用 open 函数")
        print("="*80)
        print(f"\nAgent 返回结果:\n{result}")
        
        # Verify file was actually modified.
        with open(test_file_path, 'r', encoding='utf-8') as f:
            final_content = f.read()
        
        if "测试 open 函数写入成功" in final_content:
            print("\n完全验证成功！文件已被 agent 正确修改")
            print(f"\n最终文件内容:\n{final_content}")
            return True
        else:
            print("\n警告：文件内容未按预期修改")
            return False
            
    except Exception as e:
        print("\n" + "="*80)
        print("测试失败！")
        print("="*80)
        print(f"错误信息: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up test file.
        if os.path.exists(test_file_path):
            os.remove(test_file_path)
            print(f"\n测试文件已清理: {test_file_path}")

if __name__ == "__main__":
    print("\n" + "="*80)
    print("Test goal: verify whether the agent can use Python `open()`.")
    print("="*80 + "\n")
    
    success = test_open_function()
    
    sys.exit(0 if success else 1)
