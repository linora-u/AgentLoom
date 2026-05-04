"""Tests for the allow-model: "force-inject" skill feature.

Covers:
- Tri-state allow-model parsing from SKILL.md frontmatter
- Force-injected skills excluded from on-demand catalogue prompt
- Force-injected skills full instructions appear in force_injected_prompt
- load_skill deduplication: returns short notice for force-injected skills
- Mixed mode: force-injected + on-demand skills coexist correctly
"""

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
from src.lib.smolagents.skills.parser import parse_skill_file
from src.lib.smolagents.skills.skills import SkillsManager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestAllowModelTriState(unittest.TestCase):
    """Test tri-state parsing of parse_invocation_control()."""

    def _parse(self, allow_model_value) -> any:
        """Helper: call parse_invocation_control with a given allow-model value."""
        from src.lib.smolagents.skills.parser import parse_invocation_control
        ic = parse_invocation_control({"allow-model": allow_model_value})
        return ic["allow-model"]

    def test_true_default(self):
        """When allow-model is not set, defaults to True."""
        from src.lib.smolagents.skills.parser import parse_invocation_control
        ic = parse_invocation_control({})
        self.assertIs(ic["allow-model"], True)

    def test_true_explicit(self):
        self.assertIs(self._parse(True), True)

    def test_false_explicit(self):
        self.assertIs(self._parse(False), False)

    def test_force_inject_hyphen(self):
        self.assertEqual(self._parse("force-inject"), "force-inject")

    def test_force_inject_underscore(self):
        self.assertEqual(self._parse("force_inject"), "force-inject")

    def test_force_inject_short(self):
        self.assertEqual(self._parse("inject"), "force-inject")

    def test_force_inject_case_insensitive(self):
        self.assertEqual(self._parse("Force-Inject"), "force-inject")
        self.assertEqual(self._parse("FORCE-INJECT"), "force-inject")

    def test_bool_variants_still_work(self):
        """Truthy/falsy string variants still parse correctly."""
        self.assertIs(self._parse("yes"), True)
        self.assertIs(self._parse("no"), False)
        self.assertIs(self._parse(1), True)
        self.assertIs(self._parse(0), False)


class TestForceInject(unittest.TestCase):
    """Test force-inject behaviour via invocation-control.allow-model."""

    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=HookManager(),
        )
        self._temp_dirs = []

    def tearDown(self):
        clear_current_skills_manager()
        for td in self._temp_dirs:
            td.cleanup()

    def _write_temp_skill(self, content: str, filename: str = "skill.md") -> Path:
        td = tempfile.TemporaryDirectory()
        self._temp_dirs.append(td)
        fp = Path(td.name) / filename
        fp.write_text(content, encoding="utf-8")
        return fp

    # -- defaults -------------------------------------------------------------

    def test_allow_model_defaults_to_true(self):
        content = "---\nname: normal-skill\ndescription: Normal\n---\n# Body\n"
        skill_path = self._write_temp_skill(content)
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        self.assertIs(skill.metadata.invocation_control["allow-model"], True)

    def test_allow_model_force_inject_from_parameter(self):
        content = "---\nname: injected-skill\ndescription: Injected\n---\n# Injected body\n"
        skill_path = self._write_temp_skill(content)
        ic = {"allow-model": "force-inject", "allow-hook": True}
        skill = self.skills_manager.load_skill_metadata(str(skill_path), invocation_control=ic)
        self.assertEqual(skill.metadata.invocation_control["allow-model"], "force-inject")

    def test_allow_model_can_be_set_programmatically(self):
        content = "---\nname: injected-skill\ndescription: Injected\n---\n# Injected body\n"
        skill_path = self._write_temp_skill(content)
        skill = self.skills_manager.load_skill_metadata(str(skill_path))
        skill.metadata.invocation_control["allow-model"] = "force-inject"
        self.assertEqual(skill.metadata.invocation_control["allow-model"], "force-inject")

    # -- get_force_injected_names ---------------------------------------------

    def test_get_force_injected_names_empty_by_default(self):
        content = "---\nname: normal\ndescription: Normal\n---\n# Body\n"
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)))
        self.assertEqual(self.skills_manager.get_force_injected_names(), set())

    def test_get_force_injected_names_returns_flagged_skills(self):
        content_a = "---\nname: skill-a\ndescription: A\n---\n# A body\n"
        content_b = "---\nname: skill-b\ndescription: B\n---\n# B body\n"
        ic_inject = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_a)), invocation_control=ic_inject)
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_b)))
        self.assertEqual(self.skills_manager.get_force_injected_names(), {"skill-a"})

    # -- get_force_injected_prompt -------------------------------------------

    def test_force_injected_prompt_empty_when_no_forced(self):
        content = "---\nname: normal\ndescription: Normal\n---\n# Body\n"
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)))
        self.assertEqual(self.skills_manager.get_force_injected_prompt(), "")

    def test_force_injected_prompt_contains_full_instructions(self):
        content = "---\nname: my-skill\ndescription: My description\n---\n# Full instructions here\n"
        ic = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)), invocation_control=ic)

        prompt = self.skills_manager.get_force_injected_prompt()
        self.assertIn("<force_injected_skills>", prompt)
        self.assertIn('force_injected_skill name="my-skill"', prompt)
        self.assertIn("<description>My description</description>", prompt)
        self.assertIn("# Full instructions here", prompt)
        self.assertIn("</force_injected_skills>", prompt)

    # -- get_skills_prompt excludes force-injected ----------------------------

    def test_skills_prompt_excludes_force_injected(self):
        content_a = "---\nname: forced\ndescription: Forced skill\n---\n# Body A\n"
        content_b = "---\nname: normal\ndescription: Normal skill\n---\n# Body B\n"
        ic = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_a)), invocation_control=ic)
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_b)))

        prompt = self.skills_manager.get_skills_prompt()
        self.assertNotIn("<name>forced</name>", prompt)
        self.assertIn("<name>normal</name>", prompt)

    def test_skills_prompt_empty_when_all_force_injected(self):
        content = "---\nname: only-skill\ndescription: Only\n---\n# Body\n"
        ic = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)), invocation_control=ic)

        prompt = self.skills_manager.get_skills_prompt()
        self.assertEqual(prompt, "")

    def test_list_skills_still_shows_force_injected_skills(self):
        content = "---\nname: injected\ndescription: Injected\n---\n# Body\n"
        ic = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)), invocation_control=ic)

        set_current_skills_manager(self.skills_manager)
        data = json.loads(list_skills())
        names = {item["name"] for item in data}
        self.assertIn("injected", names)

    # -- load_skill deduplication ---------------------------------------------

    def test_load_skill_returns_dedup_notice_for_force_injected(self):
        content = "---\nname: injected\ndescription: Injected\n---\n# Full body\n"
        ic = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)), invocation_control=ic)

        set_current_skills_manager(self.skills_manager)
        result = skill_tool("injected")

        self.assertIn("<skill_already_loaded>", result)
        self.assertIn("force-injected", result)
        self.assertNotIn("# Full body", result)

    def test_load_skill_returns_full_body_for_non_force_injected(self):
        content = "---\nname: normal\ndescription: Normal\n---\n# Full body\n"
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content)))

        set_current_skills_manager(self.skills_manager)
        result = skill_tool("normal")

        self.assertNotIn("<skill_already_loaded>", result)
        self.assertIn("# Full body", result)

    # -- Mixed mode -----------------------------------------------------------

    def test_mixed_forced_and_on_demand_skills(self):
        """When some skills are force-injected and others are not, both paths work."""
        content_forced = "---\nname: skill-forced\ndescription: Forced\n---\n# Forced instructions\n"
        content_normal = "---\nname: skill-normal\ndescription: Normal\n---\n# Normal instructions\n"

        ic = {"allow-model": "force-inject", "allow-hook": True}
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_forced)), invocation_control=ic)
        self.skills_manager.load_skill_metadata(str(self._write_temp_skill(content_normal)))

        set_current_skills_manager(self.skills_manager)

        # Force-injected prompt has forced skill full body
        fi_prompt = self.skills_manager.get_force_injected_prompt()
        self.assertIn("# Forced instructions", fi_prompt)
        self.assertNotIn("# Normal instructions", fi_prompt)

        # Catalogue prompt only lists normal skill
        catalogue = self.skills_manager.get_skills_prompt()
        self.assertIn("<name>skill-normal</name>", catalogue)
        self.assertNotIn("<name>skill-forced</name>", catalogue)

        # load_skill dedup works for forced
        result_forced = skill_tool("skill-forced")
        self.assertIn("<skill_already_loaded>", result_forced)

        # load_skill still returns full body for normal
        result_normal = skill_tool("skill-normal")
        self.assertIn("# Normal instructions", result_normal)


if __name__ == "__main__":
    unittest.main()
