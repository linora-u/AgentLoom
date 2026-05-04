#!/usr/bin/env python3
import os
import sys
import fire
import arrow
from pathlib import Path

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

AGENT_LOOM_ROOT = project_root

from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent
from src.trace import generate_id, get_current_sub_task_id, get_current_task_id
from smolagents.models import ChatMessage, MessageRole
from smolagents.agents import MultiStepAgent
from src.lib.smolagents.memory.context_compression import ContextBudgetConfig, ConversationHistoryManager

# --- PATCH START: Monkey Patch ConversationHistoryManager to force max_tokens ---
_original_init = ConversationHistoryManager.__init__

def _patched_init(self, max_tokens: int = 200000):
    """Force `max_tokens` to 3000."""
    _original_init(self, max_tokens=3000)

ConversationHistoryManager.__init__ = _patched_init
# --- PATCH END ---


def run_test_compression(target_dir: str = None, target_file: str = None):
    """
    Run the compression test agent.

    Args:
        target_dir: Directory to inspect (optional).
        target_file: Optional specific file to read.
    """
    # Load Agent Configuration
    current_dir = Path(__file__).parent
    yaml_path = current_dir / "workflows" / "compression_test_agent.yaml"
    config = YamlAgentFactory._load_config_from_file(yaml_path)

    # Initialize Agent (Auto Mount Workspace Here)
    supervisor = YamlConfiguredSupervisorAgent(config=config)

    # Construct Task
    task_content = f"""
帮我完成以下任务（每个任务都要生成足够多的内容以触发压缩）：

1. 列出目录 {AGENT_LOOM_ROOT} 的文件
2. 读取文件 {AGENT_LOOM_ROOT}/applications/test_demo/test_demo.py
4. 总结一下上面两个任务的执行结果

注意：对每个任务都要详细说明你看到了什么。
"""

    task_id = generate_id(task_content, prefix="task")

    try:
        result = supervisor.run(task_content, task_id=task_id)
        print("\n" + "="*80)
        print("测试完成！")
        print("="*80)

        # ========== Verify compression result ==========
        if hasattr(supervisor, '_agent') and supervisor._agent and hasattr(supervisor._agent, '_history_manager'):
            history_manager = supervisor._agent._history_manager
            internal_messages = history_manager.get_internal_messages()

            print("\n" + "="*80)
            print("📊 压缩结果统计")
            print("="*80)
            print(f"总消息数: {len(internal_messages)}")

            # Count message categories.
            visible_count = sum(1 for msg in internal_messages if msg.is_visible())
            compressed_count = sum(1 for msg in internal_messages if msg.condense_id)
            summary_count = sum(1 for msg in internal_messages if msg.is_summary)
            system_prompt_count = sum(1 for msg in internal_messages if msg.message.role == MessageRole.SYSTEM)

            print(f"可见消息: {visible_count}")
            print(f"被压缩消息: {compressed_count}")
            print(f"Summary 消息: {summary_count}")
            print(f"System prompt: {system_prompt_count}")

            # Verify whether compression is triggered.
            if summary_count > 0:
                print("\n" + "="*80)
                print("✅ 成功触发压缩！")
                print("="*80)

                # Check whether system prompt is preserved.
                has_system_prompt = system_prompt_count > 0
                print(f"\n{'✅' if has_system_prompt else '❌'} System prompt 保留: {has_system_prompt}")

                # Check whether command blocks are preserved.
                has_command_blocks = False
                for msg in internal_messages:
                    if msg.is_summary:
                        content_str = str(msg.message.content) if msg.message.content else ""
                        if "<command name=\"test_compression\">" in content_str:
                            has_command_blocks = True
                            break

                print(f"{'✅' if has_command_blocks else '❌'} Command blocks 保留: {has_command_blocks}")

                # Check caching behavior.
                cached_blocks = history_manager._cached_command_blocks
                has_cache = bool(cached_blocks)
                print(f"{'✅' if has_cache else '❌'} Command blocks 缓存: {has_cache}")

                # Print cached content.
                if has_cache and cached_blocks:
                    print("\n" + "="*80)
                    print("💾 缓存的 Command Blocks:")
                    print("="*80)
                    print(cached_blocks)

                # Print detailed info for all messages.
                print("\n" + "="*80)
                print(f"📝 完整消息列表 (共 {len(internal_messages)} 条):")
                print("="*80)

                for i, msg in enumerate(internal_messages):
                    role = msg.message.role
                    is_visible = "可见" if msg.is_visible() else "不可见"
                    is_summary = "Summary" if msg.is_summary else "普通"

                    content = msg.message.content
                    # Extract full content (handle multiple content parts).
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict):
                                text_parts.append(part.get('text', ''))
                            else:
                                text_parts.append(str(part))
                        text = '\n'.join(text_parts)
                    else:
                        text = str(content) if content else ""

                    # Show full content for summary messages; truncate others.
                    if msg.is_summary:
                        preview = text  # 完整显示
                    else:
                        preview = text[:200] + "..." if len(text) > 200 else text

                    print(f"\n消息 #{i}: [{role}] [{is_visible}] [{is_summary}]")
                    print(f"  内容: {preview}")
                    if msg.condense_id:
                        print(f"  压缩ID: {msg.condense_id}")

                # Print final messages after compression.
                print("\n" + "="*80)
                print("📤 压缩后发送给LLM的消息:")
                print("="*80)

                compressed_messages = history_manager.get_compressed_messages(
                    model_id=getattr(supervisor._agent.model, "model_id", None),
                    logger=supervisor._agent.logger
                )

                print(f"压缩后消息数: {len(compressed_messages)}")
                for i, msg in enumerate(compressed_messages):
                    # msg is a ChatMessage object, not a dict.
                    role = msg.role
                    content = msg.content

                    if isinstance(content, list) and len(content) > 0:
                        first_part = content[0]
                        if isinstance(first_part, dict):
                            text = first_part.get('text', '')
                        else:
                            text = str(first_part)
                    else:
                        text = str(content) if content else ""

                    # Print full text for all messages.
                    print(f"\n消息 #{i}: [{role}]")
                    print(f"{text}")

            else:
                print("\n" + "="*80)
                print("⚠️  未触发压缩")
                print("="*80)
        else:
            print("\n⚠️  无法访问 _agent._history_manager")

    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Run compression test.
    run_test_compression()
