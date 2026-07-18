import json
import logging
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

from src.tools.skills import load_skill as skill_tool, list_skills, read_skill_resource
from src.trace.task_context import (
    clear_current_skills_manager,
    set_current_skills_manager,
)
from src.lib.smolagents.skills.skills import SkillsManager


def _reset_singletons():
    SkillsManager._instance = None


class TestSkillTool(unittest.TestCase):
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

    def test_skill_tool_returns_instructions(self):
        content = (
            "---\n"
            "name: demo-skill\n"
            "description: Demo\n"
            "---\n"
            "# Body\n"
        )
        skill_path = self._write_temp_skill(content)
        self.skills_manager.load_skill_metadata(str(skill_path))

        result = skill_tool("demo-skill")
        self.assertIn("<skill_name>demo-skill</skill_name>", result)
        self.assertIn("<description>Demo</description>", result)
        self.assertIn("<instructions>", result)
        self.assertIn("# Body", result)

    def test_skill_tool_lazy_loads_body_without_persistent_activation(self):
        content = (
            "---\n"
            "name: lazy-skill\n"
            "description: Lazy demo\n"
            "---\n"
            "# Body\n"
        )
        skill_path = self._write_temp_skill(content)
        self.skills_manager.load_skill_metadata(str(skill_path))

        stored_skill = self.skills_manager.skills["lazy-skill"]
        self.assertIsNone(stored_skill.content)
        self.assertFalse(hasattr(self.skills_manager, "build_hook_handlers"))
        self.assertFalse(hasattr(self.skills_manager, "active_skills"))

        result = skill_tool("lazy-skill")

        self.assertIn("# Body", result)
        self.assertEqual(stored_skill.content, "# Body\n")
        self.assertFalse(hasattr(stored_skill.metadata, "hooks"))
        self.assertFalse(hasattr(self.skills_manager, "active_skills"))

    def test_skill_tool_unknown_skill(self):
        with self.assertRaises(ValueError) as ctx:
            skill_tool("missing-skill")
        self.assertIn("Skill 'missing-skill' not found", str(ctx.exception))

    def test_list_skills_returns_json(self):
        content = (
            "---\n"
            "name: list-skill\n"
            "description: List demo\n"
            "---\n"
            "# Body\n"
        )
        skill_path = self._write_temp_skill(content)
        self.skills_manager.load_skill_metadata(str(skill_path))

        data = json.loads(list_skills())
        names = {item["name"] for item in data}
        self.assertIn("list-skill", names)
        item = next((i for i in data if i["name"] == "list-skill"), None)
        self.assertIsNotNone(item)
        self.assertEqual(item.get("description"), "List demo")

    def test_skill_tool_prefers_current_skills_manager_context(self):
        skill_a_path = self._write_temp_skill("---\nname: skill-a\ndescription: A\n---\n# Body A\n")
        skill_b_path = self._write_temp_skill("---\nname: skill-b\ndescription: B\n---\n# Body B\n")

        manager_a = SkillsManager(logger=logging.getLogger(__name__))
        manager_b = SkillsManager(logger=logging.getLogger(__name__))
        manager_a.load_skill_metadata(str(skill_a_path))
        manager_b.load_skill_metadata(str(skill_b_path))

        set_current_skills_manager(manager_a)
        result = skill_tool("skill-a")
        self.assertIn("# Body A", result)
        with self.assertRaises(ValueError):
            skill_tool("skill-b")

        set_current_skills_manager(manager_b)
        result = skill_tool("skill-b")
        self.assertIn("# Body B", result)
        with self.assertRaises(ValueError):
            skill_tool("skill-a")

    def test_list_skills_prefers_current_skills_manager_context(self):
        skill_a_path = self._write_temp_skill("---\nname: ctx-a\ndescription: A\n---\n# A\n")
        skill_b_path = self._write_temp_skill("---\nname: ctx-b\ndescription: B\n---\n# B\n")

        manager_a = SkillsManager(logger=logging.getLogger(__name__))
        manager_b = SkillsManager(logger=logging.getLogger(__name__))
        manager_a.load_skill_metadata(str(skill_a_path))
        manager_b.load_skill_metadata(str(skill_b_path))

        set_current_skills_manager(manager_a)
        data = json.loads(list_skills())
        self.assertEqual([item["name"] for item in data], ["ctx-a"])

        set_current_skills_manager(manager_b)
        data = json.loads(list_skills())
        self.assertEqual([item["name"] for item in data], ["ctx-b"])

    def test_list_skills_uses_context_fallback_across_threads(self):
        skill_path = self._write_temp_skill("---\nname: threaded-skill\ndescription: Threaded\n---\n# Body\n")
        self.skills_manager.load_skill_metadata(str(skill_path))
        set_current_skills_manager(self.skills_manager)

        with ThreadPoolExecutor(max_workers=1) as executor:
            data = json.loads(executor.submit(list_skills).result())

        self.assertEqual([item["name"] for item in data], ["threaded-skill"])

    def test_read_skill_resource_reads_bundled_file_by_lines(self):
        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        skill_dir = Path(temp_dir.name) / "resource-skill"
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: resource-skill\ndescription: Resource\n---\n# Body\n",
            encoding="utf-8",
        )
        (skill_dir / "references" / "guide.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.skills_manager.load_skill_metadata(str(skill_dir / "SKILL.md"))

        data = json.loads(read_skill_resource("resource-skill", "references/guide.md", offset=2, limit=1))

        self.assertEqual(data["content"], "two")
        self.assertEqual(data["total_lines"], 3)

    def test_read_skill_resource_rejects_directory_escape(self):
        temp_dir = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temp_dir)
        skill_dir = Path(temp_dir.name) / "resource-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: escape-skill\ndescription: Escape\n---\n# Body\n",
            encoding="utf-8",
        )
        self.skills_manager.load_skill_metadata(str(skill_dir / "SKILL.md"))

        with self.assertRaises(ValueError):
            read_skill_resource("escape-skill", "../outside.txt")


if __name__ == "__main__":
    unittest.main()
