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

    def _write_temp_skill(self, content: str, filename: str = "SKILL.md") -> Path:
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
            "argument-hint: '<file>'\n"
            "arguments: [file]\n"
            "when_to_use: Use when sample work applies.\n"
            "---\n"
            "# Title\n"
            "Hello\n"
        )
        skill_path = self._write_temp_skill(content)

        skill = self.skills_manager.load_skill_metadata(str(skill_path))

        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, "sample-skill")
        self.assertEqual(skill.metadata.description, "Sample skill")
        self.assertEqual(skill.metadata.version, "1.0")
        self.assertEqual(skill.metadata.allowed_tools, ["Read", "Write"])
        self.assertEqual(skill.metadata.argument_hint, "<file>")
        self.assertEqual(skill.metadata.arguments, ["file"])
        self.assertEqual(skill.metadata.when_to_use, "Use when sample work applies.")
        self.assertEqual(skill.metadata.load_mode, "on-demand")
        self.assertTrue(skill.metadata.allow_scripts)
        self.assertTrue(skill.metadata.allow_network)
        self.assertIsNone(skill.content)

        loaded = self.skills_manager.get_skill_content("sample-skill")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.instructions, "# Title\nHello\n")

    def test_missing_frontmatter_raises(self):
        with self.assertRaisesRegex(ValueError, "requires non-empty string field 'name'"):
            self.skills_manager.load_skill_metadata(str(self._write_temp_skill("Hello\n")))

    def test_invalid_yaml_raises(self):
        with self.assertRaisesRegex(ValueError, "Invalid skill frontmatter"):
            self.skills_manager.load_skill_metadata(str(self._write_temp_skill("---\n: bad\n---\n# Body\n")))

    def test_missing_name_raises(self):
        with self.assertRaisesRegex(ValueError, "requires non-empty string field 'name'"):
            self.skills_manager.load_skill_metadata(
                str(self._write_temp_skill("---\ndescription: no name\n---\n# Body\n"))
            )

    def test_missing_description_raises(self):
        with self.assertRaisesRegex(ValueError, "requires non-empty string field 'description'"):
            self.skills_manager.load_skill_metadata(
                str(self._write_temp_skill("---\nname: sample-skill\n---\n# Body\n"))
            )

    def test_invalid_allowed_tools_type_raises(self):
        with self.assertRaisesRegex(ValueError, "allowed-tools"):
            self.skills_manager.load_skill_metadata(
                str(self._write_temp_skill("---\nname: sample-skill\ndescription: Sample\nallowed-tools: 123\n---\n# Body\n"))
            )

    def test_invalid_hooks_type_raises(self):
        with self.assertRaisesRegex(ValueError, "hooks"):
            self.skills_manager.load_skill_metadata(
                str(self._write_temp_skill("---\nname: sample-skill\ndescription: Sample\nhooks:\n  - 1\n---\n# Body\n"))
            )

    def test_unknown_and_legacy_frontmatter_fields_are_ignored(self):
        content = (
            "---\n"
            "name: sample-skill\n"
            "description: Sample skill\n"
            "runtime: runtime.py\n"
            "when-to-use: legacy should be ignored\n"
            "argument-names: [legacy]\n"
            "disable-model-invocation: true\n"
            "user-invocable: false\n"
            "requires:\n"
            "  bins: [python]\n"
            "---\n"
            "# Body\n"
        )

        skill = self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)))

        self.assertIsNotNone(skill)
        self.assertIsNone(skill.metadata.when_to_use)
        self.assertIsNone(skill.metadata.arguments)
        self.assertFalse(hasattr(skill.metadata, "runtime"))
        self.assertFalse(hasattr(skill.metadata, "requires"))

    def test_get_skills_prompt_returns_catalogue_only(self):
        content = (
            "---\n"
            "name: sample-skill\n"
            "description: Sample skill\n"
            "argument-hint: '<task>'\n"
            "when_to_use: Use for samples.\n"
            "---\n"
            "# Body\n"
            "Detailed skill body\n"
        )
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)))

        prompt = self.skills_manager.get_skills_prompt()

        self.assertIn("<available_skills>", prompt)
        self.assertIn("<name>sample-skill</name>", prompt)
        self.assertIn("<description>Sample skill</description>", prompt)
        self.assertIn("<argument_hint>&lt;task&gt;</argument_hint>".replace("&lt;task&gt;", "<task>"), prompt)
        self.assertIn("<when_to_use>Use for samples.</when_to_use>", prompt)
        self.assertNotIn("Detailed skill body", prompt)

    def test_eager_skill_excluded_from_catalogue_and_injected(self):
        content = "---\nname: eager-skill\ndescription: Eager\n---\n# Eager body\n"
        self.skills_manager.load_skill_metadata(
            str(self._write_temp_skill(content)),
            load_mode="eager",
        )

        self.assertEqual(self.skills_manager.get_skills_prompt(), "")
        eager_prompt = self.skills_manager.get_eager_skills_prompt()
        self.assertIn("<eager_loaded_skills>", eager_prompt)
        self.assertIn('eager_loaded_skill name="eager-skill"', eager_prompt)
        self.assertIn("# Eager body", eager_prompt)

    def test_same_manager_rejects_same_skill_name_from_different_paths(self):
        path_a = self._write_temp_skill("---\nname: duplicated\ndescription: A\n---\n# A\n")
        path_b = self._write_temp_skill("---\nname: duplicated\ndescription: B\n---\n# B\n")

        self.skills_manager.load_skill_metadata(str(path_a))
        with self.assertRaisesRegex(ValueError, "Duplicate skill name"):
            self.skills_manager.load_skill_metadata(str(path_b))

    def test_same_manager_reloading_same_skill_path_is_idempotent_and_updates_policy(self):
        skill_path = self._write_temp_skill("---\nname: duplicated\ndescription: Same\n---\n# A\n")

        first = self.skills_manager.load_skill_metadata(str(skill_path))
        second = self.skills_manager.load_skill_metadata(
            str(skill_path),
            load_mode="eager",
            allow_scripts=False,
            allow_network=False,
        )

        self.assertIs(first, second)
        self.assertEqual(len(self.skills_manager.skills), 1)
        self.assertEqual(second.metadata.load_mode, "eager")
        self.assertFalse(second.metadata.allow_scripts)
        self.assertFalse(second.metadata.allow_network)

    def test_different_managers_allow_same_skill_name_from_different_paths(self):
        path_a = self._write_temp_skill("---\nname: same-name\ndescription: A\n---\n# A\n")
        path_b = self._write_temp_skill("---\nname: same-name\ndescription: B\n---\n# B\n")

        manager_a = SkillsManager(logger=logging.getLogger(__name__), hook_manager=HookManager())
        manager_b = SkillsManager(logger=logging.getLogger(__name__), hook_manager=HookManager())

        skill_a = manager_a.load_skill_metadata(str(path_a))
        skill_b = manager_b.load_skill_metadata(str(path_b))

        self.assertIsNotNone(skill_a)
        self.assertIsNotNone(skill_b)
        self.assertEqual(skill_a.file_path, str(path_a.absolute()))
        self.assertEqual(skill_b.file_path, str(path_b.absolute()))


if __name__ == "__main__":
    unittest.main()
