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


class TestSkillsDirectoryLoading(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))

    def test_directory_loading_recursively_loads_skill_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            (root / "SKILL.md").write_text("---\nname: root-skill\n---\n# Root\n", encoding="utf-8")

            alpha = root / "alpha"
            alpha.mkdir()
            (alpha / "skill.md").write_text("---\nname: alpha-skill\n---\n# A\n", encoding="utf-8")

            beta = root / "beta"
            beta.mkdir()
            (beta / "skill.md").write_text("---\nname: beta-skill\n---\n# B\n", encoding="utf-8")
            (beta / "skills.md").write_text("---\nname: beta-skills\n---\n# B2\n", encoding="utf-8")

            gamma = root / "gamma"
            gamma.mkdir()
            (gamma / "SKILLS.MD").write_text("---\nname: gamma-skills\n---\n# G\n", encoding="utf-8")

            delta = root / "delta"
            delta.mkdir()
            nested = delta / "nested"
            nested.mkdir()
            (nested / "skill.md").write_text("---\nname: nested-skill\n---\n# N\n", encoding="utf-8")
            (nested / "skills.md").write_text("---\nname: nested-skills\n---\n# N2\n", encoding="utf-8")

            misc = root / "misc"
            misc.mkdir()
            (misc / "note.md").write_text("---\nname: misc\n---\n# Misc\n", encoding="utf-8")

            self.skills_manager.load_skills_from_directory(str(root))

            self.assertIn("root-skill", self.skills_manager.skills)
            self.assertIn("alpha-skill", self.skills_manager.skills)
            self.assertIn("beta-skill", self.skills_manager.skills)
            self.assertIn("beta-skills", self.skills_manager.skills)
            self.assertIn("gamma-skills", self.skills_manager.skills)
            self.assertIn("nested-skill", self.skills_manager.skills)
            self.assertIn("nested-skills", self.skills_manager.skills)
            self.assertNotIn("misc", self.skills_manager.skills)


if __name__ == "__main__":
    unittest.main()
