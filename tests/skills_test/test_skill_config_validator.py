import logging
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

from src.lib.smolagents.skills.skills import SkillsManager


def _reset_singletons():
    SkillsManager._instance = None


class TestSkillConfigValidator(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))

    def test_documented_support_matrix_matches_current_parser_behavior(self):
        with tempfile.TemporaryDirectory(prefix="skill-parser-alignment-") as temp_dir:
            skill_path = Path(temp_dir) / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: parser-alignment\n"
                "description: Parser fixture\n"
                "version: \"1.0\"\n"
                "allowed-tools: Read, Write\n"
                "argument-hint: \"<path>\"\n"
                "arguments: [path]\n"
                "when_to_use: Use when inspecting files.\n"
                "model: powerful\n"
                "context: fork\n"
                "agent: reviewer\n"
                "effort: high\n"
                "shell: bash\n"
                "hooks:\n"
                "  PreToolUse:\n"
                "    - matcher: \"Read|Write\"\n"
                "      hooks:\n"
                "        - type: command\n"
                "          command: \"echo hi\"\n"
                "platform: Claude\n"
                "custom-field: ignored\n"
                "when-to-use: legacy ignored\n"
                "argument-names: [legacy]\n"
                "---\n"
                "# Body\n",
                encoding="utf-8",
            )

            skill = self.skills_manager.load_skill_metadata(str(skill_path))
            self.assertIsNotNone(skill)
            self.assertEqual(skill.metadata.name, "parser-alignment")
            self.assertEqual(skill.metadata.description, "Parser fixture")
            self.assertEqual(skill.metadata.version, "1.0")
            self.assertEqual(skill.metadata.allowed_tools, ["Read", "Write"])
            self.assertEqual(skill.metadata.argument_hint, "<path>")
            self.assertEqual(skill.metadata.arguments, ["path"])
            self.assertEqual(skill.metadata.when_to_use, "Use when inspecting files.")
            self.assertEqual(skill.metadata.model, "powerful")
            self.assertEqual(skill.metadata.context, "fork")
            self.assertEqual(skill.metadata.agent, "reviewer")
            self.assertEqual(skill.metadata.effort, "high")
            self.assertEqual(skill.metadata.shell, "bash")
            self.assertIn("PreToolUse", skill.metadata.hooks)
            self.assertIsNone(skill.metadata.platform)
            self.assertFalse(hasattr(skill.metadata, "argument_names"))
            self.assertFalse(hasattr(skill.metadata, "runtime"))
            self.assertFalse(hasattr(skill.metadata, "custom-field"))


if __name__ == "__main__":
    unittest.main()
