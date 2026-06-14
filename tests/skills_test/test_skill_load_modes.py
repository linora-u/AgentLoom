import json
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
from src.tools.skills import list_skills, load_skill
from src.trace.task_context import clear_current_skills_manager, set_current_skills_manager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestSkillLoadModes(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=HookManager(),
        )
        self._temp_dirs = []
        set_current_skills_manager(self.skills_manager)

    def tearDown(self):
        clear_current_skills_manager()
        for td in self._temp_dirs:
            td.cleanup()

    def _write_temp_skill(self, content: str, filename: str = "SKILL.md") -> Path:
        td = tempfile.TemporaryDirectory()
        self._temp_dirs.append(td)
        fp = Path(td.name) / filename
        fp.write_text(content, encoding="utf-8")
        return fp

    def test_on_demand_is_default(self):
        path = self._write_temp_skill("---\nname: normal-skill\ndescription: Normal\n---\n# Body\n")

        skill = self.skills_manager.load_skill_metadata(str(path))

        self.assertEqual(skill.metadata.load_mode, "on-demand")
        self.assertIn("<name>normal-skill</name>", self.skills_manager.get_skills_prompt())
        self.assertIn("# Body", load_skill("normal-skill"))

    def test_eager_load_mode_injects_body_and_skips_catalogue(self):
        path = self._write_temp_skill("---\nname: eager-skill\ndescription: Eager\n---\n# Eager body\n")

        self.skills_manager.load_skill_metadata(str(path), load_mode="eager")

        self.assertEqual(self.skills_manager.get_skills_prompt(), "")
        eager = self.skills_manager.get_eager_skills_prompt()
        self.assertIn("<eager_loaded_skills>", eager)
        self.assertIn("# Eager body", eager)
        self.assertIn("already been eagerly loaded", load_skill("eager-skill"))

    def test_all_configured_skills_are_listed(self):
        path = self._write_temp_skill("---\nname: eager-skill\ndescription: Eager\n---\n# Eager body\n")
        self.skills_manager.load_skill_metadata(str(path), load_mode="eager")

        data = json.loads(list_skills(detail="full"))

        self.assertEqual(data[0]["name"], "eager-skill")
        self.assertEqual(data[0]["load_mode"], "eager")


if __name__ == "__main__":
    unittest.main()
