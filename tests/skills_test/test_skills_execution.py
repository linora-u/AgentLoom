import logging
import os
import shutil
import sys
import unittest
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

from src.lib.smolagents.skills.skills import SkillsManager
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookEvent


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestSkillsIntegration(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))
        self.hook_manager = HookManager.get_instance()

        source_skill_dir = AGENT_LOOM_ROOT / "skills" / "agent-recall-with-files"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)
        # Point hook scripts to the temp dir as project root so that
        # _find_agent_loom_root() resolves correctly in subprocess hooks.
        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        if not source_skill_dir.exists():
            self.fail(f"Skill directory not found at {source_skill_dir}")

        self.skill_dir = Path(self.temp_dir.name) / "agent-recall-with-files"
        shutil.copytree(source_skill_dir, self.skill_dir)
        self.skill_path = self.skill_dir / "SKILL.md"

        # Create agent-scoped runtime directory under the project root
        # (AGENT_LOOM_RUNTIME_ROOT points to temp_dir, so .runtime/ resolves there).
        self.runtime_dir = Path(self.temp_dir.name) / ".runtime" / "default"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        # Set STEP_NUMBER high enough to bypass the PostToolUse grace period
        # (the freshness-driven reminder engine is silent for steps <= 3).
        os.environ["STEP_NUMBER"] = "10"

        (self.runtime_dir / "trace.md").write_text(
            "# Trace\n\n"
            "recent-trace-note\n",
            encoding="utf-8",
        )
        (self.runtime_dir / "insights.md").write_text(
            "# Insights\n\n"
            "important-insight-note\n",
            encoding="utf-8",
        )
        (self.runtime_dir / "context.md").write_text(
            "# Context\n\n"
            "current-context-note\n",
            encoding="utf-8",
        )

    def tearDown(self):
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.environ.pop("STEP_NUMBER", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_real_skill_hooks(self):
        skill = self.skills_manager.load_skill_metadata(str(self.skill_path))
        self.assertIsNotNone(skill, "Failed to load SKILL.md")
        self.assertEqual(skill.metadata.name, "agent-recall-with-files")
        self.assertIsNone(skill.content)

        pre_result_empty = self.hook_manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE,
            "Write",
            {"file": "test.txt", "content": "foo"},
        )
        self.assertFalse(hasattr(pre_result_empty, "message"))

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)

        pre_result = self.hook_manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE,
            "Write",
            {"file": "test.txt", "content": "foo"},
        )
        self.assertIn("recent-trace-note", pre_result.agent_context or "")
        self.assertIn("important-insight-note", pre_result.agent_context or "")
        self.assertIn("current-context-note", pre_result.agent_context or "")
        self.assertIsNone(pre_result.user_message)

        post_result = self.hook_manager.trigger_hooks(
            HookEvent.POST_TOOL_USE,
            "Write",
            {"file": "test.txt", "content": "foo"},
        )
        self.assertIsNone(post_result.user_message)
        # PostToolUse now uses freshness-driven reminders: files with real
        # content that were just detected as "fresh" produce no reminder.
        # The hook stays silent when all tracked files are up-to-date.
        self.assertEqual(post_result.decision, "allow")

        stop_result = self.hook_manager.trigger_hooks(
            HookEvent.STOP,
            "Write",
            {"final_answer": "done"},
        )
        self.assertEqual(stop_result.decision, "allow")
        self.assertIn("allow", (stop_result.reason or "").lower())

    def test_stop_hook_ignores_legacy_task_plan_content(self):
        Path("task_plan.md").write_text(
            "# Task Plan\n\ninvalid legacy content\n",
            encoding="utf-8",
        )

        skill = self.skills_manager.load_skill_metadata(str(self.skill_path))
        self.assertIsNotNone(skill, "Failed to load SKILL.md")

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)

        stop_result = self.hook_manager.trigger_hooks(
            HookEvent.STOP,
            "Write",
            {"final_answer": "done"},
        )

        self.assertEqual(stop_result.decision, "allow")
        self.assertIn("allow", (stop_result.reason or "").lower())

    def test_pre_tool_rewrites_runtime_alias_to_current_agent(self):
        skill = self.skills_manager.load_skill_metadata(str(self.skill_path))
        self.assertIsNotNone(skill, "Failed to load SKILL.md")

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)

        pre_result = self.hook_manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE,
            "Write",
            {
                "agent_name": "default",
                "file_path": ".runtime/supervisor/trace.md",
            },
        )

        self.assertEqual(pre_result.decision, "modify")
        self.assertEqual(
            pre_result.modified_input["file_path"],
            ".runtime/default/trace.md",
        )
        self.assertIn("exact current agent directory", pre_result.agent_context or "")

    def test_pre_tool_rewrites_runtime_alias_inside_shell_commands(self):
        skill = self.skills_manager.load_skill_metadata(str(self.skill_path))
        self.assertIsNotNone(skill, "Failed to load SKILL.md")

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)

        pre_result = self.hook_manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE,
            "Write",
            {
                "agent_name": "project_scan",
                "commands": [
                    "rm -rf .runtime/worker/",
                    "mkdir -p .runtime/worker/",
                ],
            },
        )

        self.assertEqual(pre_result.decision, "modify")
        self.assertEqual(
            pre_result.modified_input["commands"],
            [
                "rm -rf .runtime/project_scan/",
                "mkdir -p .runtime/project_scan/",
            ],
        )


if __name__ == "__main__":
    unittest.main()
