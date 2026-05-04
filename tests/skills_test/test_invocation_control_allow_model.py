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

from src.tools.skills import load_skill as skill_tool, list_skills
from src.trace.task_context import (
    clear_current_skills_manager,
    set_current_skills_manager,
)
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.skills.skills import SkillsManager

def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None

class TestAllowModel(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(logger=logging.getLogger(__name__))
        self._temp_dirs = []

    def tearDown(self):
        clear_current_skills_manager()
        for temp_dir in self._temp_dirs:
            temp_dir.cleanup()

    def _write_temp_skill(self, content: str, filename: str = "skill.md") -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        file_path = Path(temp_dir.name) / filename
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def test_list_skills_filters_disabled_skills(self):
        content_normal = "---\nname: normal-skill\ndescription: Normal\n---\n"
        content_disabled = "---\nname: hidden-skill\ndescription: Hidden\n---\n"
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_normal)))
        ic_hidden = {"allow-model": False, "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_disabled)), invocation_control=ic_hidden)
        
        set_current_skills_manager(self.skills_manager)
        
        data = json.loads(list_skills())
        names = {item["name"] for item in data}
        
        self.assertIn("normal-skill", names)
        self.assertNotIn("hidden-skill", names)

    def test_load_skill_rejects_disabled_skills(self):
        content_normal = "---\nname: normal-skill\ndescription: Normal\n---\n"
        content_disabled = "---\nname: hidden-skill\ndescription: Hidden\n---\n"
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_normal)))
        ic_hidden = {"allow-model": False, "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_disabled)), invocation_control=ic_hidden)
        
        set_current_skills_manager(self.skills_manager)
        
        # Should work for normal skill
        skill_tool("normal-skill")
        
        # Should raise error for disabled skill
        with self.assertRaises(ValueError) as ctx:
            skill_tool("hidden-skill")
            
        error_msg = str(ctx.exception)
        self.assertIn("Skill 'hidden-skill' not found", error_msg)
        self.assertIn("normal-skill", error_msg)
        self.assertNotIn("hidden-skill", error_msg.split("Available skills:")[1])

if __name__ == "__main__":
    unittest.main()
