import logging
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookEvent
from src.lib.smolagents.skills.skills import SkillsManager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestSkillsLoading(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))
        self._temp_dirs = []

    def tearDown(self):
        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def test_skills_manager_always_has_logger_adapter(self):
        manager = SkillsManager(logger=None, hook_manager=HookManager())
        self.assertIsNotNone(manager._logger)

        manager.set_logger(None)
        self.assertIsNotNone(manager._logger)

    def _write_temp_skill(self, content: str, filename: str = "skill.md") -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        file_path = Path(temp_dir.name) / filename
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def test_load_skill_metadata_only(self):
        content = (
            "---\n"
            "name: sample-skill\n"
            "description: Sample skill\n"
            "version: \"1.0\"\n"
            "allowed-tools:\n"
            "  - Read\n"
            "  - Write\n"
            "---\n"
            "# Title\n"
            "Hello\n"
        )
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())

        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, "sample-skill")
        self.assertEqual(skill.metadata.description, "Sample skill")
        self.assertEqual(skill.metadata.version, "1.0")
        # Default invocation_control when not passed
        self.assertTrue(skill.metadata.invocation_control.get("allow-model"))
        self.assertEqual(skill.metadata.allowed_tools, ["Read", "Write"])
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content("sample-skill")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, "# Title\nHello\n")

    def test_load_skill_missing_frontmatter_returns_none(self):
        content = "Hello\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, skill_path.parent.name)
        self.assertEqual(skill.metadata.description, "")
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, content)

    def test_load_skill_invalid_yaml_returns_none(self):
        content = "---\n: bad\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, skill_path.parent.name)
        self.assertEqual(skill.metadata.description, "")
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, content)

    def test_load_skill_missing_name_returns_none(self):
        content = "---\ndescription: no name\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, skill_path.parent.name)
        self.assertEqual(skill.metadata.description, "no name")
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, "# Body\n")

    def test_load_skill_name_non_string_uses_parent(self):
        content = "---\nname: 123\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, skill_path.parent.name)
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, "# Body\n")

    def test_load_skill_description_and_version_non_string_defaulted(self):
        content = "---\nname: sample-skill\ndescription: 123\nversion:\n  - v1\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, "sample-skill")
        self.assertEqual(skill.metadata.description, "")
        self.assertIsNone(skill.metadata.version)
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, "# Body\n")

    def test_load_skill_allowed_tools_string_wrapped(self):
        content = "---\nname: sample-skill\nallowed-tools: Read\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.allowed_tools, ["Read"])
        self.assertIsNone(skill.content)

    def test_load_skill_allowed_tools_invalid_type_none(self):
        content = "---\nname: sample-skill\nallowed-tools: 123\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertIsNone(skill.metadata.allowed_tools)
        self.assertIsNone(skill.content)

    def test_load_skill_hooks_invalid_type_none(self):
        content = "---\nname: sample-skill\nhooks:\n  - 1\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertIsNone(skill.metadata.hooks)
        self.assertIsNone(skill.content)

    def test_load_skill_ignores_unknown_runtime_frontmatter(self):
        content = "---\nname: sample-skill\ndescription: Sample skill\nruntime: runtime.py\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertFalse(hasattr(skill.metadata, "runtime"))

    def test_invocation_control_defaults_when_not_passed(self):
        """Without invocation_control param, defaults to allow-model=True, allow-hook=True."""
        content = "---\nname: sample-skill\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertTrue(skill.metadata.invocation_control.get("allow-model"))
        self.assertTrue(skill.metadata.invocation_control.get("allow-hook"))

    def test_invocation_control_passed_via_parameter(self):
        """invocation_control passed as parameter is applied to metadata."""
        content = "---\nname: sample-skill\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        ic = {"allow-model": False, "allow-hook": False}
        skill = self.skills_manager.load_skill_metadata(str(skill_path), invocation_control=ic)
        self.assertIsNotNone(skill)
        self.assertIs(skill.metadata.invocation_control.get("allow-model"), False)
        self.assertIs(skill.metadata.invocation_control.get("allow-hook"), False)

    def test_allow_hook_false_skips_hook_registration(self):
        content = (
            "---\n"
            "name: no-hook-skill\n"
            "hooks:\n"
            "  TaskStart:\n"
            "    - hooks:\n"
            "        - type: unsupported-type\n"
            "          command: \"echo should-not-run\"\n"
            "---\n"
            "# Body\n"
        )
        skill_path = self._write_temp_skill(content)
        ic = {"allow-model": True, "allow-hook": False}
        skill = self.skills_manager.load_skill_metadata(str(skill_path), invocation_control=ic)
        self.assertIsNotNone(skill)
        self.assertTrue(skill.hooks_registered)
        self.assertEqual(len(self.skills_manager.hook_manager.hooks[HookEvent.TASK_CREATED]), 0)

    def test_load_skill_frontmatter_in_middle_ignored(self):
        content = "Hello\n---\nname: mid\n---\nWorld\n"
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, skill_path.parent.name)
        self.assertEqual(skill.metadata.description, "")
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, content)

    def test_frontmatter_does_not_close_on_indented_dashes(self):
        content = (
            "---\n"
            "name: indent-skill\n"
            "description: |\n"
            "  line1\n"
            "  ---\n"
            "  line2\n"
            "---\n"
            "# Body\n"
        )
        skill_path = self._write_temp_skill(content)
        self.assertTrue(skill_path.exists())
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, "indent-skill")
        self.assertEqual(skill.metadata.description, "line1\n---\nline2\n")
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, "# Body\n")

    def test_get_skills_prompt_returns_catalogue_only(self):
        content = (
            "---\n"
            "name: sample-skill\n"
            "description: Sample skill\n"
            "---\n"
            "# Body\n"
            "Detailed skill body\n"
        )
        skill_path = self._write_temp_skill(content)
        self.skills_manager.load_skill_metadata(str(skill_path))
        prompt = self.skills_manager.get_skills_prompt()
        self.assertIn("<available_skills>", prompt)
        self.assertIn("<name>sample-skill</name>", prompt)
        self.assertIn("<description>Sample skill</description>", prompt)
        self.assertIn("<mandatory_skill_check>", prompt)
        self.assertIn("<linked_file_handling>", prompt)
        self.assertNotIn("ACTIVE SKILL RULES", prompt)
        self.assertNotIn("### Skill:", prompt)
        self.assertNotIn("Detailed skill body", prompt)

    def test_same_manager_overrides_same_skill_name_from_different_paths(self):
        """Agent-level skill overrides global skill with same name (warn, not error)."""
        path_a = self._write_temp_skill("---\nname: duplicated\n---\n# A\n")
        path_b = self._write_temp_skill("---\nname: duplicated\n---\n# B\n")

        self.skills_manager.load_skill_metadata(str(path_a))
        # Should NOT raise; instead warn and override
        with self.assertLogs(level="WARNING") as cm:
            skill_b = self.skills_manager.load_skill_metadata(str(path_b))
        self.assertIsNotNone(skill_b)
        # The skill dict now points to path_b (override)
        self.assertEqual(self.skills_manager.skills["duplicated"].file_path, str(path_b.resolve()))
        self.assertTrue(any("overriding" in msg for msg in cm.output))

    def test_same_manager_reloading_same_skill_path_is_idempotent(self):
        skill_path = self._write_temp_skill("---\nname: duplicated\n---\n# A\n")

        first = self.skills_manager.load_skill_metadata(str(skill_path))
        second = self.skills_manager.load_skill_metadata(str(skill_path))

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(self.skills_manager.skills), 1)

    def test_different_managers_allow_same_skill_name_from_different_paths(self):
        path_a = self._write_temp_skill("---\nname: same-name\n---\n# A\n")
        path_b = self._write_temp_skill("---\nname: same-name\n---\n# B\n")

        manager_a = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=HookManager(),
        )
        manager_b = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=HookManager(),
        )

        skill_a = manager_a.load_skill_metadata(str(path_a))
        skill_b = manager_b.load_skill_metadata(str(path_b))

        self.assertIsNotNone(skill_a)
        self.assertIsNotNone(skill_b)
        self.assertEqual(skill_a.file_path, str(path_a))
        self.assertEqual(skill_b.file_path, str(path_b))


if __name__ == "__main__":
    unittest.main()
