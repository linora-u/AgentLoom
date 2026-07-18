import json
import logging
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.lib.runtime import RuntimeHome, bind_run_context
from src.lib.smolagents.skills.skills import SkillsManager
from src.tools.skills import run_skill_script as run_skill_script_tool
from src.trace.task_context import clear_current_skills_manager, set_current_skills_manager


def _reset_singletons():
    SkillsManager._instance = None


class TestSkillScriptRuntime(unittest.TestCase):
    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runtime_context = RuntimeHome(self.root / ".agentloom").context(
            application_id="skill-tests",
            task_id="task",
            run_id="run",
        )
        self._runtime_binding = bind_run_context(self.runtime_context)
        self._runtime_binding.__enter__()
        self.skills_manager = SkillsManager(logger=logging.getLogger(__name__))
        set_current_skills_manager(self.skills_manager)

    def tearDown(self):
        clear_current_skills_manager()
        self._runtime_binding.__exit__(None, None, None)
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
        self.assertTrue(audit_dir.is_relative_to(self.runtime_context.skill_artifacts_dir))
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

    def test_large_output_is_streamed_to_artifact_with_bounded_preview(self):
        skill_path = self._write_skill("large-output")
        script = skill_path.parent / "scripts" / "large.py"
        output_size = 2 * 1024 * 1024
        script.write_text(
            f'import sys\nsys.stdout.write("x" * {output_size})\n',
            encoding="utf-8",
        )
        self.skills_manager.load_skill_metadata(str(skill_path))

        result = self.skills_manager.run_skill_script(
            "large-output",
            f"{shlex.quote(sys.executable)} scripts/large.py",
            timeout=10,
        )

        stdout_path = Path(result["stdout_path"])
        self.assertEqual(result["stdout_bytes"], output_size)
        self.assertTrue(result["stdout_preview_truncated"])
        self.assertEqual(len(result["stdout_preview"].encode("utf-8")), 4000)
        self.assertEqual(stdout_path.stat().st_size, output_size)

    def test_timeout_keeps_output_in_current_run_artifact(self):
        skill_path = self._write_skill("timeout-output")
        script = skill_path.parent / "scripts" / "timeout.py"
        child_code = "import time; time.sleep(1.5); print('late-child', flush=True)"
        script.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\n"
            'sys.stdout.write("before-timeout\\n")\n'
            "sys.stdout.flush()\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        self.skills_manager.load_skill_metadata(str(skill_path))

        result = self.skills_manager.run_skill_script(
            "timeout-output",
            f"{shlex.quote(sys.executable)} scripts/timeout.py",
            timeout=1,
        )

        self.assertTrue(result["timed_out"])
        self.assertEqual(result["stdout_preview"], "before-timeout\n")
        stdout_path = Path(result["stdout_path"])
        self.assertEqual(stdout_path.read_text(encoding="utf-8"), "before-timeout\n")
        stable_size = stdout_path.stat().st_size
        time.sleep(0.8)
        self.assertEqual(stdout_path.stat().st_size, stable_size)


if __name__ == "__main__":
    unittest.main()
