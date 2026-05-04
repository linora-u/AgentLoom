"""Tests for reloading skills with allow-model: "force-inject" set in SKILL.md."""

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

class TestForceInjectReloading(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=HookManager(),
        )

    def test_force_inject_preserved_on_reloading_same_directory(self):
        """When a skill with allow-model: force-inject is reloaded from
        the same directory, the existing skill object is returned and the
        force-inject setting is preserved."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            skill_dir = root / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "skill.md").write_text(
                "---\nname: my-skill\ndescription: Test\n---\n# Body\n",
                encoding="utf-8"
            )

            ic = {"allow-model": "force-inject", "allow-hook": True}

            # Layer 1: Initial load
            loaded_names_1 = self.skills_manager.load_skills_from_directory(
                str(skill_dir), invocation_control=ic,
            )

            self.assertEqual(len(loaded_names_1), 1)
            self.assertEqual(loaded_names_1[0], "my-skill")

            skill = self.skills_manager.skills["my-skill"]
            self.assertEqual(
                skill.metadata.invocation_control["allow-model"],
                "force-inject",
            )

            # Layer 3: Reloading same directory — returns existing skill
            loaded_names_2 = self.skills_manager.load_skills_from_directory(
                str(skill_dir), invocation_control=ic,
            )

            self.assertEqual(len(loaded_names_2), 1)
            self.assertEqual(
                self.skills_manager.skills["my-skill"].metadata.invocation_control["allow-model"],
                "force-inject",
            )

    def test_on_demand_skill_stays_on_demand_on_reload(self):
        """A normal on-demand skill stays on-demand after reload."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "normal-skill"
            skill_dir.mkdir()
            (skill_dir / "skill.md").write_text(
                "---\nname: normal-skill\ndescription: Test\n---\n# Body\n",
                encoding="utf-8"
            )

            self.skills_manager.load_skills_from_directory(str(skill_dir))
            self.assertIs(
                self.skills_manager.skills["normal-skill"].metadata.invocation_control["allow-model"],
                True,
            )

            self.skills_manager.load_skills_from_directory(str(skill_dir))
            self.assertIs(
                self.skills_manager.skills["normal-skill"].metadata.invocation_control["allow-model"],
                True,
            )

if __name__ == "__main__":
    unittest.main()
