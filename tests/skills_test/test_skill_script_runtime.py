import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.skills.skills import SkillsManager
from src.tools.skills import run_skill_script as run_skill_script_tool
from src.trace.task_context import clear_current_skills_manager, set_current_skills_manager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestSkillScriptRuntime(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=HookManager(),
        )
        set_current_skills_manager(self.skills_manager)

    def tearDown(self):
        clear_current_skills_manager()
        self.temp_dir.cleanup()

    def _write_skill(self, name: str = "script-skill") -> Path:
        skill_dir = self.root / name
        (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(
            "---\n"
            f"name: {name}\n"
            "description: Script runtime fixture\n"
            "---\n"
            "# Script fixture\n",
            encoding="utf-8",
        )
        return skill_path

    def test_run_skill_script_executes_and_records_audit(self):
        skill_path = self._write_skill()
        script = skill_path.parent / "scripts" / "write.sh"
        script.write_text(
            'printf "artifact" > "$AGENTLOOM_SKILL_WORKSPACE/result.txt"\n'
            'printf "skill-dir=%s\\n" "$CLAUDE_SKILL_DIR"\n',
            encoding="utf-8",
        )
        self.skills_manager.load_skill_metadata(str(skill_path))

        result = self.skills_manager.run_skill_script(
            "script-skill",
            "sh scripts/write.sh",
            timeout=10,
        )

        self.assertFalse(result["blocked"])
        self.assertEqual(result["returncode"], 0)
        self.assertIn("skill-dir=", result["stdout_preview"])
        audit_dir = Path(result["audit_dir"])
        self.assertTrue((audit_dir / "audit.json").is_file())
        self.assertTrue((audit_dir / "stdout.txt").is_file())
        self.assertEqual((audit_dir.parent.parent / "result.txt").read_text(encoding="utf-8"), "artifact")

    def test_tool_run_skill_script_returns_json(self):
        skill_path = self._write_skill()
        self.skills_manager.load_skill_metadata(str(skill_path))

        payload = json.loads(run_skill_script_tool("script-skill", "printf tool-ok", timeout=10))

        self.assertFalse(payload["blocked"])
        self.assertEqual(payload["returncode"], 0)
        self.assertIn("tool-ok", payload["stdout_preview"])

    def test_allow_scripts_false_blocks_execution_but_writes_audit(self):
        skill_path = self._write_skill("no-scripts")
        self.skills_manager.load_skill_metadata(str(skill_path), allow_scripts=False)

        result = self.skills_manager.run_skill_script("no-scripts", "printf should-not-run")

        self.assertTrue(result["blocked"])
        self.assertIn("script execution is disabled", result["blocked_reason"])
        self.assertTrue((Path(result["audit_dir"]) / "audit.json").is_file())

    def test_allow_network_false_blocks_common_network_commands(self):
        skill_path = self._write_skill("no-network")
        self.skills_manager.load_skill_metadata(str(skill_path), allow_network=False)

        result = self.skills_manager.run_skill_script("no-network", "curl https://example.com")

        self.assertTrue(result["blocked"])
        self.assertIn("network command 'curl' is blocked", result["blocked_reason"])
        self.assertTrue((Path(result["audit_dir"]) / "audit.json").is_file())

    def test_env_allowlist_restricts_extra_environment(self):
        skill_path = self._write_skill("env-skill")
        self.skills_manager.load_skill_metadata(str(skill_path))
        old_visible = os.environ.get("AGENTLOOM_TEST_VISIBLE")
        old_hidden = os.environ.get("AGENTLOOM_TEST_HIDDEN")
        os.environ["AGENTLOOM_TEST_VISIBLE"] = "visible"
        os.environ["AGENTLOOM_TEST_HIDDEN"] = "hidden"
        try:
            result = self.skills_manager.run_skill_script(
                "env-skill",
                'test "$AGENTLOOM_TEST_VISIBLE" = visible && '
                'test -z "$AGENTLOOM_TEST_HIDDEN" && printf env-ok',
                env_allowlist="AGENTLOOM_TEST_VISIBLE",
            )
        finally:
            if old_visible is None:
                os.environ.pop("AGENTLOOM_TEST_VISIBLE", None)
            else:
                os.environ["AGENTLOOM_TEST_VISIBLE"] = old_visible
            if old_hidden is None:
                os.environ.pop("AGENTLOOM_TEST_HIDDEN", None)
            else:
                os.environ["AGENTLOOM_TEST_HIDDEN"] = old_hidden

        self.assertFalse(result["blocked"])
        self.assertEqual(result["returncode"], 0)
        self.assertIn("env-ok", result["stdout_preview"])
        self.assertIn("AGENTLOOM_TEST_VISIBLE", result["env_names"])
        self.assertNotIn("AGENTLOOM_TEST_HIDDEN", result["env_names"])


if __name__ == "__main__":
    unittest.main()
