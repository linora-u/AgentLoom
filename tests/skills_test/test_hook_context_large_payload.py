"""Tests for the large-payload tempfile mechanism in shell hook execution.

Covers the [Errno 7] error scenario from repo_map_agent_20260326_202800.log:
  - Verifies the tempfile + truncated env-var dual approach in executors.py
  - Verifies common.py get_hook_context() read-priority (file > env-var)
  - Verifies tempfile cleanup after execution
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

from src.lib.smolagents.hooks.types import HookContext, HookEvent
from src.lib.smolagents.skills.executors import create_hook_executor
from src.lib.smolagents.skills.skills import SkillsManager
from src.lib.smolagents.hooks.hook_manager import HookManager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


def _make_context(tool_input: dict, tool_response=None) -> HookContext:
    """Build a minimal HookContext for testing."""
    return HookContext(
        session_id="test-session",
        cwd="/tmp",
        hook_event_name="PreToolUse",
        tool_name="Write",
        tool_input=tool_input,
        tool_response=tool_response,
    )


class TestShellExecutorTempFile(unittest.TestCase):
    """Verify that the shell executor uses tempfile for large payloads."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        # Create a trivial hook script that reads HOOK_CONTEXT_JSON_FILE
        # and reports back what it read from.
        self.script_path = os.path.join(self.tmp_dir, "echo_hook.py")
        with open(self.script_path, "w") as f:
            f.write(
                '#!/usr/bin/env python3\n'
                'import json, os\n'
                'result = {"decision": "allow"}\n'
                'json_file = os.environ.get("HOOK_CONTEXT_JSON_FILE", "")\n'
                'if json_file and os.path.isfile(json_file):\n'
                '    with open(json_file) as fh:\n'
                '        ctx = json.load(fh)\n'
                '    result["telemetry"] = {\n'
                '        "read_from": "file",\n'
                '        "tool_input_len": len(json.dumps(ctx.get("tool_input", {}))),\n'
                '    }\n'
                'else:\n'
                '    raw = os.environ.get("HOOK_CONTEXT_JSON", "")\n'
                '    result["telemetry"] = {\n'
                '        "read_from": "env",\n'
                '        "env_len": len(raw),\n'
                '    }\n'
                'print(json.dumps(result))\n'
            )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_hook(self, tool_input: dict, tool_response=None):
        """Create and invoke a shell hook executor with the given payload."""
        python_bin = sys.executable
        command = f"{python_bin} {self.script_path}"
        executor = create_hook_executor(
            code=command,
            skill_name="test-skill",
            skill_dir=self.tmp_dir,
            logger=logging.getLogger(__name__),
            timeout=30,
        )
        ctx = _make_context(tool_input, tool_response)
        return executor(ctx)

    def test_small_payload_succeeds(self):
        """Normal-sized payloads should work with HOOK_CONTEXT_JSON_FILE."""
        result = self._run_hook({"file": "test.txt", "content": "hello"})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.telemetry.get("read_from"), "file")

    def test_bare_python_command_uses_active_runtime_path(self):
        """Skill hook commands using bare `python` should work without a user shim."""
        executor = create_hook_executor(
            code=f"python {self.script_path}",
            skill_name="test-skill",
            skill_dir=self.tmp_dir,
            logger=logging.getLogger(__name__),
            timeout=30,
        )
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            result = executor(_make_context({"file": "test.txt"}))

        self.assertTrue(result.success, f"Hook failed: {result.telemetry}")
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.telemetry.get("read_from"), "file")

    def test_large_payload_no_errno7(self):
        """Payloads >64KB must NOT cause [Errno 7] Argument list too long.

        Before the fix this would fail with OSError: [Errno 7]
        because the entire JSON was stuffed into an environment variable.
        """
        large_content = "x" * (200 * 1024)  # 200KB
        result = self._run_hook(
            {"file": "big.txt", "content": large_content},
        )
        self.assertTrue(result.success, f"Hook failed: {result.telemetry}")
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.telemetry.get("read_from"), "file")
        # The file should contain the full tool_input (not truncated)
        self.assertGreater(result.telemetry.get("tool_input_len", 0), 200_000)

    def test_large_tool_response_no_errno7(self):
        """Large tool_response should also be handled via tempfile."""
        large_response = {"output": "y" * (200 * 1024)}
        result = self._run_hook(
            {"file": "test.txt"},
            tool_response=large_response,
        )
        self.assertTrue(result.success, f"Hook failed: {result.telemetry}")
        self.assertEqual(result.decision, "allow")

    def test_tempfile_cleaned_up_after_execution(self):
        """The temporary JSON file must be deleted after hook execution."""
        tmp_files_before = set(
            f for f in os.listdir(tempfile.gettempdir())
            if f.startswith("hook_ctx_") and f.endswith(".json")
        )

        self._run_hook({"file": "test.txt", "content": "a" * (100 * 1024)})

        tmp_files_after = set(
            f for f in os.listdir(tempfile.gettempdir())
            if f.startswith("hook_ctx_") and f.endswith(".json")
        )
        leaked = tmp_files_after - tmp_files_before
        self.assertEqual(
            len(leaked), 0,
            f"Temp files leaked after hook execution: {leaked}"
        )


class TestCommonGetHookContext(unittest.TestCase):
    """Verify common.py get_hook_context() read priority: file > env-var."""

    def _import_common_get_hook_context(self):
        """Import get_hook_context from the skill scripts directory."""
        common_dir = AGENT_LOOM_ROOT / "skills" / "agent-recall-with-files" / "scripts"
        if str(common_dir) not in sys.path:
            sys.path.insert(0, str(common_dir))
        if "common" in sys.modules:
            del sys.modules["common"]
        from common import get_hook_context
        return get_hook_context

    def test_prefers_file_over_env_var(self):
        """When both HOOK_CONTEXT_JSON_FILE and HOOK_CONTEXT_JSON are set,
        the file should take precedence."""
        file_payload = {"session_id": "from-file", "tool_input": {"big": True}}
        env_payload = {"session_id": "from-env", "tool_input": {"big": False}}

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(file_payload, tmp)
        tmp.close()

        try:
            env_patch = {
                "HOOK_CONTEXT_JSON_FILE": tmp.name,
                "HOOK_CONTEXT_JSON": json.dumps(env_payload),
            }
            with patch.dict(os.environ, env_patch):
                get_hook_context = self._import_common_get_hook_context()
                result = get_hook_context()

            self.assertEqual(result["session_id"], "from-file")
            self.assertTrue(result["tool_input"]["big"])
        finally:
            os.remove(tmp.name)

    def test_falls_back_to_env_var_when_file_missing(self):
        """When HOOK_CONTEXT_JSON_FILE points to a non-existent path,
        fall back to HOOK_CONTEXT_JSON."""
        env_payload = {"session_id": "from-env", "tool_name": "Read"}

        env_patch = {
            "HOOK_CONTEXT_JSON_FILE": "/tmp/nonexistent_hook_ctx.json",
            "HOOK_CONTEXT_JSON": json.dumps(env_payload),
        }
        with patch.dict(os.environ, env_patch):
            get_hook_context = self._import_common_get_hook_context()
            result = get_hook_context()

        self.assertEqual(result["session_id"], "from-env")
        self.assertEqual(result["tool_name"], "Read")

    def test_falls_back_to_env_var_when_no_file_var(self):
        """When HOOK_CONTEXT_JSON_FILE is not set, read from env-var."""
        env_payload = {"session_id": "env-only", "tool_name": "Bash"}

        env_patch = {
            "HOOK_CONTEXT_JSON": json.dumps(env_payload),
        }
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("HOOK_CONTEXT_JSON_FILE", None)
            get_hook_context = self._import_common_get_hook_context()
            result = get_hook_context()

        self.assertEqual(result["session_id"], "env-only")

    def test_returns_empty_when_neither_set(self):
        """When neither file nor env-var is set, return empty dict."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOOK_CONTEXT_JSON_FILE", None)
            os.environ.pop("HOOK_CONTEXT_JSON", None)
            get_hook_context = self._import_common_get_hook_context()
            result = get_hook_context()

        self.assertEqual(result, {})

    def test_file_with_corrupt_json_falls_back(self):
        """If the temp file contains corrupt JSON, fall back to env-var."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        tmp.write("{corrupt json!!!}")
        tmp.close()

        env_payload = {"session_id": "env-fallback"}
        try:
            env_patch = {
                "HOOK_CONTEXT_JSON_FILE": tmp.name,
                "HOOK_CONTEXT_JSON": json.dumps(env_payload),
            }
            with patch.dict(os.environ, env_patch):
                get_hook_context = self._import_common_get_hook_context()
                result = get_hook_context()

            self.assertEqual(result["session_id"], "env-fallback")
        finally:
            os.remove(tmp.name)


class TestLargePayloadIntegration(unittest.TestCase):
    """End-to-end: large payload through real agent-recall-with-files hooks."""

    def setUp(self):
        _reset_singletons()
        self.skills_manager = SkillsManager.get_instance(
            logger=logging.getLogger(__name__)
        )
        self.hook_manager = HookManager.get_instance()

        source_skill_dir = AGENT_LOOM_ROOT / "skills" / "agent-recall-with-files"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)
        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        self.skill_dir = Path(self.temp_dir.name) / "agent-recall-with-files"
        shutil.copytree(source_skill_dir, self.skill_dir)
        self.skill_path = self.skill_dir / "SKILL.md"

        self.runtime_dir = Path(self.temp_dir.name) / ".runtime" / "default"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "trace.md").write_text("# Trace\n", encoding="utf-8")
        (self.runtime_dir / "insights.md").write_text("# Insights\n", encoding="utf-8")
        (self.runtime_dir / "context.md").write_text("# Context\n", encoding="utf-8")

    def tearDown(self):
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_pre_tool_use_with_large_tool_input(self):
        """PreToolUse hook must succeed with >128KB tool_input.

        This is the exact scenario that caused [Errno 7] in production:
        agent-recall-with-files hooks failed because HOOK_CONTEXT_JSON
        exceeded the Linux ARG_MAX limit.
        """
        skill = self.skills_manager.load_skill_metadata(str(self.skill_path))
        self.assertIsNotNone(skill)
        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)

        large_content = "A" * (200 * 1024)  # 200KB
        result = self.hook_manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE,
            "Write",
            {"file": "big_output.txt", "content": large_content},
        )
        self.assertIsNotNone(result)
        self.assertIn(result.decision, ("allow", "modify"))
        self.assertIsNotNone(result.agent_context)

    def test_post_tool_use_with_large_tool_response(self):
        """PostToolUse hook must succeed with a large tool_response."""
        skill = self.skills_manager.load_skill_metadata(str(self.skill_path))
        self.assertIsNotNone(skill)
        loaded = self.skills_manager.get_skill_content(skill.metadata.name)
        self.assertIsNotNone(loaded)

        result = self.hook_manager.trigger_hooks(
            HookEvent.POST_TOOL_USE,
            "Read",
            {"file": "huge.txt"},
            tool_response="B" * (200 * 1024),
        )
        self.assertIsNotNone(result)
        self.assertIn(result.decision, ("allow", "modify"))


if __name__ == "__main__":
    unittest.main()
