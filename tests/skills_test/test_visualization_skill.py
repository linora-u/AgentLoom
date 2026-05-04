"""Tests for agent-visualization skill.

Verifies that every lifecycle hook is correctly registered, fires on the
right events, and produces frontend-compatible visualization JSON.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

from src.trace.task_context import (
    clear_current_agent_name,
    clear_current_hook_manager,
    clear_current_skills_manager,
    set_current_agent_name,
    set_current_hook_manager,
    set_current_skills_manager,
    task_context,
)
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookEvent
from src.lib.smolagents.skills.skills import SkillsManager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class TestVisualizationSkillMetadata(unittest.TestCase):
    """Test 1-3: Skill metadata loading and hook registration."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_01_skill_metadata_loads_correctly(self):
        """SKILL.md loads with correct name and hook definitions."""
        skill = self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.assertIsNotNone(skill)
        self.assertEqual(skill.metadata.name, "agent-visualization")
        self.assertIsNotNone(skill.metadata.hooks)
        # Should have 7 hook event types declared
        hook_keys = set(skill.metadata.hooks.keys())
        self.assertIn("TaskCreated", hook_keys)
        self.assertIn("TaskCompleted", hook_keys)
        self.assertIn("StopFailure", hook_keys)
        self.assertIn("SubagentStart", hook_keys)
        self.assertIn("SubagentStop", hook_keys)
        self.assertIn("PreToolUse", hook_keys)
        self.assertIn("PostToolUse", hook_keys)

    def test_02_all_hooks_registered_after_metadata_load(self):
        """Loading metadata eagerly registers all hooks."""
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        # All hooks are registered eagerly
        for event in [
            HookEvent.TASK_CREATED,
            HookEvent.TASK_COMPLETED,
            HookEvent.STOP_FAILURE,
            HookEvent.SUBAGENT_START,
            HookEvent.SUBAGENT_STOP,
            HookEvent.PRE_TOOL_USE,
            HookEvent.POST_TOOL_USE,
        ]:
            hooks = self.hook_manager.hooks.get(event, [])
            self.assertGreaterEqual(
                len(hooks), 1,
                f"Expected at least 1 hook for {event.value}, got {len(hooks)}",
            )

    def test_03_tool_hooks_registered_after_content_load(self):
        """PreToolUse/PostToolUse registered after get_skill_content()."""
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        for event in [HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE]:
            hooks = self.hook_manager.hooks.get(event, [])
            self.assertGreaterEqual(
                len(hooks), 1,
                f"Expected at least 1 hook for {event.value}, got {len(hooks)}",
            )


class TestVisualizationTaskStart(unittest.TestCase):
    """Test 4-5: TaskStart hook creates JSON and writes supervisor."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        # Set AGENT_LOOM_RUNTIME_ROOT so hooks write to temp dir
        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _viz_path(self, agent_name):
        return Path(self.temp_dir.name) / ".runtime" / agent_name / "visualization.json"

    def _read_viz(self, agent_name):
        path = self._viz_path(agent_name)
        self.assertTrue(path.exists(), f"Expected {path} to exist")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_04_task_start_creates_json_file(self):
        """TaskStart creates a valid visualization.json."""
        with task_context("viz-test-04"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "viz-test-04",
                    "cwd": os.getcwd(),
                    "task_text": "Run code quality check",
                    "agent_name": "test_supervisor",
                },
            )

        path = self._viz_path("test_supervisor")
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("config", data)
        self.assertIn("timeline", data)
        self.assertIn("agents", data["config"])

    def test_05_task_start_writes_supervisor_and_start_event(self):
        """TaskStart writes supervisor to config.agents and emits start event."""
        with task_context("viz-test-05"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "viz-test-05",
                    "cwd": os.getcwd(),
                    "task_text": "Check PduR module",
                    "agent_name": "code_review_agent",
                },
            )

        data = self._read_viz("code_review_agent")
        agents = data["config"]["agents"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["name"], "code_review_agent")
        self.assertEqual(agents[0]["type"], "supervisor")

        timeline = data["timeline"]
        self.assertGreaterEqual(len(timeline), 1)
        ev = timeline[0]
        self.assertEqual(ev["event_type"], "start")
        self.assertEqual(ev["status"], "thinking")
        self.assertEqual(ev["agent_name"], "code_review_agent")
        self.assertEqual(ev["agent_type"], "supervisor")
        self.assertIn("PduR", ev["description"])


class TestVisualizationSubtasks(unittest.TestCase):
    """Test 6-8: SubtaskStart/Finish worker discovery and events."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

        # Bootstrap supervisor first
        with task_context("viz-subtask-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "viz-subtask-test",
                    "cwd": os.getcwd(),
                    "task_text": "Test task",
                    "agent_name": "main_supervisor",
                },
            )

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _read_viz(self):
        path = Path(self.temp_dir.name) / ".runtime" / "main_supervisor" / "visualization.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_06_subtask_start_adds_worker_and_events(self):
        """SubtaskStart discovers worker, adds to config, emits agent_call + activated."""
        with task_context("viz-subtask-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_START,
                tool_name="project_scan",
                tool_input={
                    "agent_name": "project_scan",
                    "sub_task_id": "sub-001",
                },
            )

        data = self._read_viz()
        # Worker should be in config
        agent_names = [a["name"] for a in data["config"]["agents"]]
        self.assertIn("project_scan", agent_names)

        worker_entry = [a for a in data["config"]["agents"] if a["name"] == "project_scan"][0]
        self.assertEqual(worker_entry["type"], "worker")

        # Timeline should have agent_call + activated events
        timeline = data["timeline"]
        # Find agent_call event
        agent_calls = [e for e in timeline if e["event_type"] == "agent_call"]
        self.assertGreaterEqual(len(agent_calls), 1)
        self.assertEqual(agent_calls[-1]["agent_name"], "main_supervisor")
        self.assertEqual(agent_calls[-1]["status"], "waiting")
        self.assertEqual(agent_calls[-1]["target_agent"], "project_scan")

        # Find activated event
        activated = [e for e in timeline if e["event_type"] == "activated"]
        self.assertGreaterEqual(len(activated), 1)
        self.assertEqual(activated[-1]["agent_name"], "project_scan")
        self.assertEqual(activated[-1]["status"], "thinking")

    def test_07_subtask_finish_marks_worker_completed(self):
        """SubtaskFinish emits worker completed + supervisor agent_return."""
        # First start the subtask
        with task_context("viz-subtask-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_START,
                tool_name="project_scan",
                tool_input={
                    "agent_name": "project_scan",
                    "sub_task_id": "sub-001",
                },
            )

        # Then finish it
        with task_context("viz-subtask-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_STOP,
                tool_name="project_scan",
                tool_input={
                    "agent_name": "project_scan",
                    "sub_task_id": "sub-001",
                    "success": True,
                },
            )

        data = self._read_viz()
        timeline = data["timeline"]

        # Find completed event for worker
        completed = [e for e in timeline if e["event_type"] == "completed" and e["agent_name"] == "project_scan"]
        self.assertGreaterEqual(len(completed), 1)
        self.assertEqual(completed[-1]["status"], "completed")

        # Find agent_return event for supervisor
        returns = [e for e in timeline if e["event_type"] == "agent_return"]
        self.assertGreaterEqual(len(returns), 1)
        self.assertEqual(returns[-1]["agent_name"], "main_supervisor")
        self.assertEqual(returns[-1]["status"], "reviewing")

    def test_08_multiple_subtasks_build_full_topology(self):
        """Multiple SubtaskStarts build config with 1 supervisor + N workers, no dupes."""
        workers = ["project_scan", "coding_standards", "error_handling"]

        for i, worker in enumerate(workers):
            with task_context("viz-subtask-test"):
                self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_START,
                    tool_name=worker,
                    tool_input={
                        "agent_name": worker,
                        "sub_task_id": f"sub-{i:03d}",
                    },
                )
            with task_context("viz-subtask-test"):
                self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_STOP,
                    tool_name=worker,
                    tool_input={
                        "agent_name": worker,
                        "sub_task_id": f"sub-{i:03d}",
                        "success": True,
                    },
                )

        data = self._read_viz()
        agents = data["config"]["agents"]
        agent_names = [a["name"] for a in agents]

        # 1 supervisor + 3 workers
        self.assertEqual(len(agents), 4, f"Expected 4 agents, got {agents}")
        self.assertIn("main_supervisor", agent_names)
        for w in workers:
            self.assertIn(w, agent_names)

        # No duplicates
        self.assertEqual(len(agent_names), len(set(agent_names)))

        # Verify types
        sup = [a for a in agents if a["name"] == "main_supervisor"][0]
        self.assertEqual(sup["type"], "supervisor")
        for w in workers:
            entry = [a for a in agents if a["name"] == w][0]
            self.assertEqual(entry["type"], "worker")


class TestVisualizationToolUse(unittest.TestCase):
    """Test 9-10: PreToolUse events and internal tool filtering."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

        # Bootstrap supervisor
        with task_context("viz-tool-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "viz-tool-test",
                    "cwd": os.getcwd(),
                    "task_text": "Test",
                    "agent_name": "test_sup",
                },
            )

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _read_viz(self):
        path = Path(self.temp_dir.name) / ".runtime" / "test_sup" / "visualization.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_09_pre_tool_use_emits_tool_call_event(self):
        """PreToolUse on shell_tool emits tool_call event; does not block."""
        with task_context("viz-tool-test"):
            set_current_agent_name("test_sup")
            try:
                result = self.hook_manager.trigger_hooks(
                    HookEvent.PRE_TOOL_USE,
                    tool_name="shell_tool",
                    tool_input={"commands": ["ls -la"]},
                )
            finally:
                clear_current_agent_name()

        # Hook should not block
        self.assertFalse(result.should_block())

        data = self._read_viz()
        timeline = data["timeline"]
        tool_calls = [e for e in timeline if e["event_type"] == "tool_call"]
        self.assertGreaterEqual(len(tool_calls), 1)
        last_tc = tool_calls[-1]
        self.assertEqual(last_tc["tool_name"], "shell_tool")
        self.assertEqual(last_tc["status"], "codeact")

    def test_10_internal_hooks_filtered(self):
        """PreToolUse on validate_workspace_path does NOT add timeline event."""
        data_before = self._read_viz()
        count_before = len(data_before["timeline"])

        with task_context("viz-tool-test"):
            set_current_agent_name("test_sup")
            try:
                self.hook_manager.trigger_hooks(
                    HookEvent.PRE_TOOL_USE,
                    tool_name="validate_workspace_path",
                    tool_input={},
                )
            finally:
                clear_current_agent_name()

        data_after = self._read_viz()
        count_after = len(data_after["timeline"])
        self.assertEqual(count_before, count_after, "Filtered tool should not add events")


class TestVisualizationTaskComplete(unittest.TestCase):
    """Test 11-12: TaskComplete and TaskFail terminal events."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _read_viz(self, agent_name):
        path = Path(self.temp_dir.name) / ".runtime" / agent_name / "visualization.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_11_task_complete_emits_final_event(self):
        """TaskComplete emits completed event as the last timeline entry."""
        # Bootstrap
        with task_context("viz-complete"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "viz-complete",
                    "cwd": os.getcwd(),
                    "task_text": "Test",
                    "agent_name": "sup_agent",
                },
            )
        # Complete
        with task_context("viz-complete"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_COMPLETED,
                tool_name="",
                tool_input={
                    "task_id": "viz-complete",
                    "agent_name": "sup_agent",
                    "result": "All checks passed",
                },
            )

        data = self._read_viz("sup_agent")
        last_ev = data["timeline"][-1]
        self.assertEqual(last_ev["event_type"], "completed")
        self.assertEqual(last_ev["status"], "completed")
        self.assertIn("passed", last_ev["description"])

    def test_12_task_fail_emits_error_event(self):
        """TaskFail emits error event with error description."""
        # Bootstrap
        with task_context("viz-fail"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "viz-fail",
                    "cwd": os.getcwd(),
                    "task_text": "Test",
                    "agent_name": "fail_agent",
                },
            )
        # Fail
        with task_context("viz-fail"):
            self.hook_manager.trigger_hooks(
                HookEvent.STOP_FAILURE,
                tool_name="",
                tool_input={
                    "task_id": "viz-fail",
                    "agent_name": "fail_agent",
                    "error": "LLM timeout after 300s",
                },
            )

        data = self._read_viz("fail_agent")
        last_ev = data["timeline"][-1]
        self.assertEqual(last_ev["event_type"], "error")
        self.assertEqual(last_ev["status"], "error")
        self.assertIn("timeout", last_ev["description"])


class TestVisualizationEndToEnd(unittest.TestCase):
    """Test 13-14: Full lifecycle sequence and JSON format validation."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _read_viz(self, agent_name):
        path = Path(self.temp_dir.name) / ".runtime" / agent_name / "visualization.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_13_full_lifecycle_sequence(self):
        """Simulate full flow: TaskStart → SubtaskStart → tool → SubtaskFinish → TaskComplete."""
        sup = "code_review_agent"
        workers = ["project_scan", "coding_standards"]

        # 1. TaskStart
        with task_context("e2e-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED, tool_name="",
                tool_input={"task_id": "e2e-test", "cwd": os.getcwd(),
                            "task_text": "Full lifecycle test", "agent_name": sup},
            )

        # 2. PreToolUse (supervisor calls load_skill)
        with task_context("e2e-test"):
            set_current_agent_name(sup)
            try:
                self.hook_manager.trigger_hooks(
                    HookEvent.PRE_TOOL_USE, tool_name="load_skill",
                    tool_input={"skill_name": "agent-recall-with-files"},
                )
            finally:
                clear_current_agent_name()

        # 3. For each worker: SubtaskStart → PreToolUse → SubtaskFinish
        for worker in workers:
            with task_context("e2e-test"):
                self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_START, tool_name=worker,
                    tool_input={"agent_name": worker, "sub_task_id": f"sub-{worker}"},
                )
            with task_context("e2e-test"):
                set_current_agent_name(worker)
                try:
                    self.hook_manager.trigger_hooks(
                        HookEvent.PRE_TOOL_USE, tool_name="shell_tool",
                        tool_input={"commands": ["rg pattern"]},
                    )
                finally:
                    clear_current_agent_name()
            with task_context("e2e-test"):
                self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_STOP, tool_name=worker,
                    tool_input={"agent_name": worker, "sub_task_id": f"sub-{worker}",
                                "success": True},
                )

        # 4. TaskComplete
        with task_context("e2e-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_COMPLETED, tool_name="",
                tool_input={"task_id": "e2e-test", "agent_name": sup,
                            "result": "Report generated"},
            )

        data = self._read_viz(sup)

        # Verify topology: 1 supervisor + 2 workers
        agents = data["config"]["agents"]
        self.assertEqual(len(agents), 3)

        # Verify timeline has meaningful events
        timeline = data["timeline"]
        self.assertGreaterEqual(len(timeline), 8, f"Expected >=8 events, got {len(timeline)}")

        # First event is start, last is completed
        self.assertEqual(timeline[0]["event_type"], "start")
        self.assertEqual(timeline[-1]["event_type"], "completed")

        # Verify all event types present
        event_types = {e["event_type"] for e in timeline}
        self.assertIn("start", event_types)
        self.assertIn("agent_call", event_types)
        self.assertIn("activated", event_types)
        self.assertIn("tool_call", event_types)
        self.assertIn("completed", event_types)
        self.assertIn("agent_return", event_types)

    def test_14_json_format_compatible_with_frontend(self):
        """Output JSON matches the schema expected by the frontend visualizer."""
        sup = "format_test_agent"

        with task_context("fmt-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED, tool_name="",
                tool_input={"task_id": "fmt-test", "cwd": os.getcwd(),
                            "task_text": "Format check", "agent_name": sup},
            )
        with task_context("fmt-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_START, tool_name="worker1",
                tool_input={"agent_name": "worker1", "sub_task_id": "s1"},
            )
        with task_context("fmt-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_STOP, tool_name="worker1",
                tool_input={"agent_name": "worker1", "sub_task_id": "s1", "success": True},
            )
        with task_context("fmt-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_COMPLETED, tool_name="",
                tool_input={"task_id": "fmt-test", "agent_name": sup, "result": "Done"},
            )

        data = self._read_viz(sup)

        # Top-level schema
        self.assertIn("config", data)
        self.assertIn("timeline", data)
        self.assertIsInstance(data["config"], dict)
        self.assertIsInstance(data["timeline"], list)
        self.assertIn("agents", data["config"])

        # Agent schema
        for agent in data["config"]["agents"]:
            self.assertIn("name", agent)
            self.assertIn("type", agent)
            self.assertIn(agent["type"], ("supervisor", "worker"))

        # Event schema (required fields per visualization skill contract)
        required_fields = {"step", "agent_name", "agent_type", "event_type", "status", "description"}
        for ev in data["timeline"]:
            missing = required_fields - set(ev.keys())
            self.assertEqual(
                missing, set(),
                f"Event step {ev.get('step')} missing fields: {missing}",
            )
            self.assertIsInstance(ev["step"], int)
            self.assertGreater(ev["step"], 0)

        # Steps should be monotonically increasing
        steps = [e["step"] for e in data["timeline"]]
        for i in range(1, len(steps)):
            self.assertGreater(steps[i], steps[i - 1], "Steps must be monotonically increasing")


class TestVisualizationRobustness(unittest.TestCase):
    """Test 15-16: Concurrent writes and non-blocking behavior."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        os.environ["AGENT_LOOM_RUNTIME_ROOT"] = self.temp_dir.name

        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-visualization"
        self.skill_dir = Path(self.temp_dir.name) / "agent-visualization"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )
        self.skills_manager.get_skill_content("agent-visualization")

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_15_rapid_sequential_hooks_produce_valid_json(self):
        """Rapidly fire 10 hooks; each intermediate state is valid JSON."""
        sup = "rapid_agent"

        with task_context("rapid-test"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED, tool_name="",
                tool_input={"task_id": "rapid-test", "cwd": os.getcwd(),
                            "task_text": "Rapid test", "agent_name": sup},
            )

        path = Path(self.temp_dir.name) / ".runtime" / sup / "visualization.json"

        # Fire 10 tool use events rapidly
        for i in range(10):
            with task_context("rapid-test"):
                set_current_agent_name(sup)
                try:
                    self.hook_manager.trigger_hooks(
                        HookEvent.PRE_TOOL_USE,
                        tool_name=f"tool_{i}",
                        tool_input={"iteration": i},
                    )
                finally:
                    clear_current_agent_name()

            # Verify JSON is valid after each write
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)  # Would raise if invalid
            self.assertIn("timeline", data)

        # Final state has 11 events (1 start + 10 tools)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["timeline"]), 11)

    def test_16_hooks_never_block_agent_execution(self):
        """All visualization hooks return decision='allow', never blocking."""
        sup = "noblock_agent"

        with task_context("noblock-test"):
            result = self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED, tool_name="",
                tool_input={"task_id": "noblock-test", "cwd": os.getcwd(),
                            "task_text": "Test", "agent_name": sup},
            )
        self.assertFalse(result.should_block(), "TaskStart should not block")

        with task_context("noblock-test"):
            result = self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_START, tool_name="worker",
                tool_input={"agent_name": "worker", "sub_task_id": "s1"},
            )
        self.assertFalse(result.should_block(), "SubtaskStart should not block")

        with task_context("noblock-test"):
            set_current_agent_name(sup)
            try:
                result = self.hook_manager.trigger_hooks(
                    HookEvent.PRE_TOOL_USE, tool_name="shell_tool",
                    tool_input={"commands": ["echo hi"]},
                )
            finally:
                clear_current_agent_name()
        self.assertFalse(result.should_block(), "PreToolUse should not block")

        with task_context("noblock-test"):
            result = self.hook_manager.trigger_hooks(
                HookEvent.SUBAGENT_STOP, tool_name="worker",
                tool_input={"agent_name": "worker", "sub_task_id": "s1", "success": True},
            )
        self.assertFalse(result.should_block(), "SubtaskFinish should not block")

        with task_context("noblock-test"):
            result = self.hook_manager.trigger_hooks(
                HookEvent.TASK_COMPLETED, tool_name="",
                tool_input={"task_id": "noblock-test", "agent_name": sup, "result": "ok"},
            )
        self.assertFalse(result.should_block(), "TaskComplete should not block")

        with task_context("noblock-test"):
            result = self.hook_manager.trigger_hooks(
                HookEvent.STOP_FAILURE, tool_name="",
                tool_input={"task_id": "noblock-test", "agent_name": sup, "error": "boom"},
            )
        self.assertFalse(result.should_block(), "TaskFail should not block")


if __name__ == "__main__":
    unittest.main()
