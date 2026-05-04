#!/usr/bin/env python3
"""Regression tests for the repo-local agent-recall-with-files skill package."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

SKILL_DIR = AGENT_LOOM_ROOT / "skills" / "agent-recall-with-files"
SKILL_PATH = SKILL_DIR / "SKILL.md"
WORKFLOW_PATH = AGENT_LOOM_ROOT / "applications" / "test_demo" / "workflows" / "test_recall_agent.yaml"
AI_QUALITY_ANALYSIS_WORKFLOW_PATH = AGENT_LOOM_ROOT / "applications" / "ai_quality_analysis" / "workflows" / "code_review_agent.yaml"
TASK_START_SCRIPT = SKILL_DIR / "scripts" / "on_task_start.py"
STOP_SCRIPT = SKILL_DIR / "scripts" / "on_stop.py"
INSIGHTS_TEMPLATE = SKILL_DIR / "templates" / "insights.md"
TRACE_TEMPLATE = SKILL_DIR / "templates" / "trace.md"
CONTEXT_TEMPLATE = SKILL_DIR / "templates" / "context.md"


class TestAgentRecallSkillRepoAdaptation(unittest.TestCase):
    def _run_stop_hook(self) -> dict:
        """Run the on_stop.py hook script and return its JSON output."""
        self.assertTrue(STOP_SCRIPT.exists(), f"Stop script not found: {STOP_SCRIPT}")

        with tempfile.TemporaryDirectory(prefix="recall-stop-hook-") as tmp:
            env = dict(**__import__("os").environ)
            env["AGENT_NAME"] = "default"
            result = subprocess.run(
                [sys.executable, str(STOP_SCRIPT)],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(result.stdout.strip(), "Expected JSON output from on_stop.py")
            return json.loads(result.stdout)

    def test_skill_text_uses_repo_local_references(self):
        self.assertTrue(SKILL_PATH.exists(), f"Skill file not found: {SKILL_PATH}")
        content = SKILL_PATH.read_text(encoding="utf-8")
        self.assertNotIn("~/.codex", content)
        self.assertNotIn("~/.claude", content)
        self.assertNotIn("CODEX_SKILL_ROOT", content)
        self.assertNotIn("session-catchup.py", content)
        self.assertNotIn("/clear", content)
        self.assertIn("python ./scripts/on_task_start.py", content)
        self.assertIn("skills/agent-recall-with-files", content)
        self.assertNotIn("task_plan.md", content)
        self.assertNotIn(".planning/", content)

    def test_workflow_uses_canonical_skill_path(self):
        self.assertTrue(WORKFLOW_PATH.exists(), f"Workflow file not found: {WORKFLOW_PATH}")
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('path: "skills/agent-recall-with-files"', content)
        self.assertTrue(SKILL_DIR.exists(), f"Canonical skill directory not found: {SKILL_DIR}")

    def test_ai_quality_analysis_workflow_uses_recall_skill(self):
        self.assertTrue(AI_QUALITY_ANALYSIS_WORKFLOW_PATH.exists(), f"Workflow file not found: {AI_QUALITY_ANALYSIS_WORKFLOW_PATH}")
        content = AI_QUALITY_ANALYSIS_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('path: "skills/agent-recall-with-files"', content)

    def test_task_start_script_bootstraps_runtime_and_cleans_legacy(self):
        self.assertTrue(TASK_START_SCRIPT.exists(), f"TaskCreated script not found: {TASK_START_SCRIPT}")

        with tempfile.TemporaryDirectory(prefix="recall-task-start-") as tmp:
            Path(tmp, "task_plan.md").write_text("legacy root plan\n", encoding="utf-8")
            Path(tmp, "findings.md").write_text("legacy root findings\n", encoding="utf-8")
            Path(tmp, "progress.md").write_text("legacy root progress\n", encoding="utf-8")
            legacy_planning_dir = Path(tmp, ".planning", "old_agent")
            legacy_planning_dir.mkdir(parents=True, exist_ok=True)
            Path(legacy_planning_dir, "task_plan.md").write_text("legacy planning file\n", encoding="utf-8")

            env = dict(**__import__("os").environ)
            env["AGENT_NAME"] = "default"
            env["AGENT_LOOM_RUNTIME_ROOT"] = tmp
            result = subprocess.run(
                [sys.executable, str(TASK_START_SCRIPT)],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            runtime_dir = Path(tmp, ".runtime", "default")
            insights_content = Path(runtime_dir, "insights.md").read_text(encoding="utf-8").replace("\r\n", "\n")
            trace_content = Path(runtime_dir, "trace.md").read_text(encoding="utf-8").replace("\r\n", "\n")
            context_content = Path(runtime_dir, "context.md").read_text(encoding="utf-8").replace("\r\n", "\n")
            self.assertFalse(Path(tmp, ".planning").exists())
            self.assertFalse(Path(tmp, "task_plan.md").exists())
            self.assertFalse(Path(tmp, "findings.md").exists())
            self.assertFalse(Path(tmp, "progress.md").exists())
            self.assertEqual(
                INSIGHTS_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n").strip(),
                insights_content.strip(),
            )
            self.assertEqual(
                trace_content.strip(),
                TRACE_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n").strip(),
            )
            self.assertEqual(
                context_content.strip(),
                CONTEXT_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n").strip(),
            )

    def test_stop_hook_unconditionally_allows(self):
        payload = self._run_stop_hook()

        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(payload["telemetry"]["status"], "disabled")
        self.assertIn("allow", payload["reason"].lower())

    def test_ai_quality_analysis_prompt_text_avoids_supervisor_planning_paths(self):
        prompt_path = AGENT_LOOM_ROOT / "applications" / "ai_quality_analysis" / "sysprompt" / "code_agent.yaml"
        content = prompt_path.read_text(encoding="utf-8")
        self.assertNotIn(".planning/supervisor", content)
        self.assertNotIn("task_plan.md", content)


if __name__ == "__main__":
    unittest.main()
