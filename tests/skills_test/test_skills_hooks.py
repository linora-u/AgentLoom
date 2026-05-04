import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

FIXTURE_PROJECT_DIR = SCRIPT_DIR / "skills" / "test1_skill"
FIXTURE_SKILL_PATH = FIXTURE_PROJECT_DIR / "skill.md"

from src.lib.smolagents.skills.skills import SkillsManager
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookEvent


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestSkillsHooks(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))
        self.hook_manager = HookManager.get_instance()

    def test_hooks_execute_and_match_patterns(self):
        self.assertTrue(FIXTURE_PROJECT_DIR.exists())
        log_path = Path(os.getcwd()) / "hook.log"
        os.environ["HOOK_LOG"] = str(log_path)
        if log_path.exists():
            log_path.unlink()
        log_path.touch()
        start_size = 0

        try:
            self.assertTrue(FIXTURE_SKILL_PATH.exists())
            skill = self.skills_manager.load_skill_metadata(str(FIXTURE_SKILL_PATH))
            self.assertIsNotNone(skill)
            self.assertIsNone(skill.content)

            pre_result_empty = self.hook_manager.trigger_hooks(
                HookEvent.PRE_TOOL_USE,
                "Write",
                {"file": "test.txt", "content": "foo"},
            )
            self.assertFalse(hasattr(pre_result_empty, "message"))
            self.assertFalse(hasattr(pre_result_empty, "print_message"))
            self.assertFalse(hasattr(pre_result_empty, "return_message"))

            loaded = self.skills_manager.get_skill_content(skill.metadata.name)
            self.assertIsNotNone(loaded)

            pre_result = self.hook_manager.trigger_hooks(
                HookEvent.PRE_TOOL_USE,
                "Write",
                {"file": "test.txt", "content": "foo"},
            )
            print(f"[hooks-test] PRE_TOOL_USE result: {pre_result}")
            pre_text = pre_result.agent_context or ""
            self.assertIn("1111", pre_text)
            self.assertIn("222", pre_text)
            self.assertIn("lyc1111", pre_text)
            self.assertLess(pre_text.find("1111"), pre_text.find("lyc1111"))

            post_result = self.hook_manager.trigger_hooks(
                HookEvent.POST_TOOL_USE,
                "Write",
                {"file": "test.txt", "content": "foo"},
            )
            print(f"[hooks-test] POST_TOOL_USE result: {post_result}")
            post_text = post_result.agent_context or ""
            self.assertIsNone(post_result.user_message)
            self.assertIn("print-post", post_text)
            self.assertIn("lyc2222", post_text)
            self.assertLess(post_text.find("print-post"), post_text.find("lyc2222"))
            self.assertEqual(self.hook_manager.consume_pending_user_messages(), [])

            stop_result = self.hook_manager.trigger_hooks(
                HookEvent.STOP,
                "Write",
                {"file": "test.txt"},
            )
            print(f"[hooks-test] STOP result: {stop_result}")
            stop_text = stop_result.reason or ""
            self.assertIn("print-stop", stop_text)
            self.assertIn("lyc333", stop_text)
            self.assertLess(stop_text.find("print-stop"), stop_text.find("lyc333"))

            with log_path.open("rb") as f:
                all_bytes = f.read()
            new_bytes = all_bytes[start_size:]
            log_text = new_bytes.decode("utf-8", errors="replace")
            print(f"[hooks-test] hook log path: {log_path}")
            print("[hooks-test] hook log content (new):\\n" + log_text)
            self.assertIn("pre:data-line-1:demo-project:helper-ok", log_text)
            self.assertIn("post", log_text)
            self.assertIn("stop", log_text)
            self.assertNotIn("read", log_text)
        finally:
            del os.environ["HOOK_LOG"]

    def test_unknown_hook_event_warns_and_is_skipped_during_registration(self):
        with tempfile.TemporaryDirectory(prefix="skills-hooks-unknown-") as temp_dir:
            skill_path = Path(temp_dir) / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: invalid-stop-skill\n"
                "description: Invalid hook fixture\n"
                "hooks:\n"
                "  BeforeFinish:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"echo hi\"\n"
                "---\n"
                "# Body\n",
                encoding="utf-8",
            )

            self.skills_manager._logger = logging.getLogger("skills-warning-test")
            with self.assertLogs("skills-warning-test", level="WARNING") as captured:
                skill = self.skills_manager.load_skill_metadata(str(skill_path))
            
            self.assertIsNotNone(skill)
            loaded = self.skills_manager.get_skill_content(skill.metadata.name)

            self.assertIsNotNone(loaded)
            self.assertTrue(any("Unsupported hook event 'BeforeFinish'" in message for message in captured.output))

            result = self.hook_manager.trigger_hooks(
                HookEvent.STOP,
                "Write",
                {"file": "test.txt"},
            )
            self.assertEqual(result.decision, "allow")
            self.assertIsNone(result.reason)

    def test_shell_hook_plain_text_output_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="skills-hooks-legacy-shell-text-") as temp_dir:
            skill_path = Path(temp_dir) / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: invalid-shell-stdout\n"
                "description: invalid shell hook fixture\n"
                "hooks:\n"
                "  PostToolUse:\n"
                "    - matcher: \"*\"\n"
                "      hooks:\n"
                "        - type: command\n"
                "          command: \"printf 'legacy-shell-output'\"\n"
                "---\n"
                "# Body\n",
                encoding="utf-8",
            )

            self.skills_manager.load_skill_metadata(str(skill_path))
            self.skills_manager.get_skill_content("invalid-shell-stdout")

            result = self.hook_manager.trigger_hooks(
                HookEvent.POST_TOOL_USE,
                "Write",
                {"file": "test.txt", "content": "foo"},
            )

            self.assertTrue(result.should_block())
            self.assertIn("structured JSON", result.reason or "")
            self.assertIsNone(result.agent_context)
            self.assertIsNone(result.user_message)

    def test_shell_hook_with_invalid_syntax_is_rejected_at_registration(self):
        with tempfile.TemporaryDirectory(prefix="skills-hooks-invalid-shell-syntax-") as temp_dir:
            skill_path = Path(temp_dir) / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: invalid-shell-syntax\n"
                "description: invalid shell hook fixture\n"
                "hooks:\n"
                "  PostToolUse:\n"
                "    - matcher: \"*\"\n"
                "      hooks:\n"
                "        - type: command\n"
                "          command: \"echo (\"\n"
                "---\n"
                "# Body\n",
                encoding="utf-8",
            )

            # PostToolUse is a lifecycle event and is eagerly registered during
            # load_skill_metadata, so the validation error surfaces there.
            with self.assertRaises(ValueError):
                self.skills_manager.load_skill_metadata(str(skill_path))

    def test_shell_hook_legacy_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="skills-hooks-legacy-shell-json-") as temp_dir:
            skill_path = Path(temp_dir) / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: invalid-shell-json-legacy\n"
                "description: invalid shell json fixture\n"
                "hooks:\n"
                "  Stop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: >-\n"
                "            python3 -c \"import json; print(json.dumps({'message': 'legacy'}))\"\n"
                "---\n"
                "# Body\n",
                encoding="utf-8",
            )

            self.skills_manager.load_skill_metadata(str(skill_path))
            self.skills_manager.get_skill_content("invalid-shell-json-legacy")

            result = self.hook_manager.trigger_hooks(
                HookEvent.STOP,
                "Write",
                {"file": "test.txt"},
            )

            self.assertTrue(result.should_block())
            self.assertIn("unsupported", (result.reason or "").lower())


if __name__ == "__main__":
    unittest.main()

    def test_regex_matcher_fullmatch_behavior(self):
        """Verify HookManager uses re.fullmatch to prevent prefix matching bugs."""
        from src.lib.smolagents.hooks.types import HookResult
        
        # Register a hook with pattern "shell_tool"
        self.hook_manager.register_hook(
            HookEvent.PRE_TOOL_USE,
            "shell_tool",
            lambda ctx: HookResult(success=True, decision="allow", reason="shell_tool_matched")
        )
        # Register a hook with pattern "Write|Edit"
        self.hook_manager.register_hook(
            HookEvent.PRE_TOOL_USE,
            "Write|Edit",
            lambda ctx: HookResult(success=True, decision="allow", reason="write_edit_matched")
        )
        # Register a hook with wildcard pattern ".*_tool"
        self.hook_manager.register_hook(
            HookEvent.PRE_TOOL_USE,
            ".*_tool",
            lambda ctx: HookResult(success=True, decision="allow", reason="wildcard_matched")
        )
        
        # Test exact match
        res = self.hook_manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "shell_tool", {})
        self.assertIn("shell_tool_matched", res.reason or "")
        self.assertIn("wildcard_matched", res.reason or "")
        
        # Test prefix mismatch (should NOT match "shell_tool")
        res2 = self.hook_manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "shell_tool_extra", {})
        self.assertNotIn("shell_tool_matched", res2.reason or "")
        self.assertIn("wildcard_matched", res2.reason or "")
        
        # Test alternation exact match
        res3 = self.hook_manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "Write", {})
        self.assertIn("write_edit_matched", res3.reason or "")
        self.assertNotIn("shell_tool_matched", res3.reason or "")
        
        # Test alternation mismatch
        res4 = self.hook_manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "Write_File", {})
        self.assertNotIn("write_edit_matched", res4.reason or "")

    def test_eager_registration_for_all_hooks(self):
        """Verify that ALL hooks including POST_TOOL_USE_FAILURE are eagerly registered."""
        with tempfile.TemporaryDirectory(prefix="skills-hooks-eager-") as temp_dir:
            skill_path = Path(temp_dir) / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: eager-all-hooks-skill\n"
                "description: Test eager registration\n"
                "hooks:\n"
                "  PostToolUseFailure:\n"
                "    - matcher: \"*\"\n"
                "      hooks:\n"
                "        - type: command\n"
                "          command: \"echo error\"\n"
                "---\n"
                "# Body\n",
                encoding="utf-8",
            )
            
            # Load metadata only
            self.skills_manager.load_skill_metadata(str(skill_path))
            
            # Check HookManager for the events
            error_hooks = self.hook_manager.hooks.get(HookEvent.POST_TOOL_USE_FAILURE, [])
            
            self.assertEqual(len(error_hooks), 1, "PostToolUseFailure should be eagerly registered")
