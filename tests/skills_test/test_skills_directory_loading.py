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


def _skill_body(name: str, description: str = "Skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"


class TestSkillsDirectoryLoading(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))

    def test_directory_loading_recursively_loads_skill_entrypoints_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            (root / "SKILL.md").write_text(_skill_body("root-skill"), encoding="utf-8")

            alpha = root / "alpha"
            alpha.mkdir()
            (alpha / "skill.md").write_text(_skill_body("alpha-skill"), encoding="utf-8")

            beta = root / "beta"
            beta.mkdir()
            (beta / "SKILLS.MD").write_text(_skill_body("beta-skills"), encoding="utf-8")

            misc = root / "misc"
            misc.mkdir()
            (misc / "note.md").write_text(_skill_body("misc"), encoding="utf-8")

            loaded = self.skills_manager.load_skills_from_directory(str(root))

            self.assertEqual(loaded, ["root-skill"])
            self.assertIn("root-skill", self.skills_manager.skills)
            self.assertNotIn("alpha-skill", self.skills_manager.skills)
            self.assertNotIn("beta-skills", self.skills_manager.skills)
            self.assertNotIn("misc", self.skills_manager.skills)

    def test_directory_without_root_skill_recurses_into_subdirectories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            alpha = root / "alpha"
            alpha.mkdir()
            (alpha / "SKILL.md").write_text(_skill_body("alpha-skill"), encoding="utf-8")
            lower = root / "lower"
            lower.mkdir()
            (lower / "skill.md").write_text(_skill_body("lower-skill"), encoding="utf-8")

            loaded = self.skills_manager.load_skills_from_directory(str(root))

            self.assertEqual(loaded, ["alpha-skill", "lower-skill"])
            self.assertIn("alpha-skill", self.skills_manager.skills)
            self.assertIn("lower-skill", self.skills_manager.skills)

    def test_direct_file_must_be_skill_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            direct = root / "SKILL.md"
            direct.write_text(_skill_body("direct-skill"), encoding="utf-8")
            loose = root / "loose.md"
            loose.write_text(_skill_body("loose-skill"), encoding="utf-8")

            self.assertEqual(self.skills_manager.load_skills_from_directory(str(direct)), ["direct-skill"])
            self.assertEqual(self.skills_manager.load_skills_from_directory(str(loose)), [])


if __name__ == "__main__":
    unittest.main()
