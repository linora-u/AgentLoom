"""Tests for generic task lifecycle hooks (TaskStart, SubtaskStart, SubtaskFinish).

These tests verify that the HookManager correctly dispatches lifecycle events
and that the agent-recall-with-files skill's shell hooks respond properly.
"""
import logging
import os
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

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
from src.lib.smolagents.agent.base_agent import BaseAgent, SubTaskTrackedAgent
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookEvent
from src.lib.smolagents.skills.skills import SkillsManager


def _reset_singletons():
    SkillsManager._instance = None
    HookManager._instance = None


class _LifecycleTestAgent(BaseAgent):
    tool_call_type = "code_act"
    max_steps = 3

    @property
    def name(self) -> str:
        return "lifecycle_test_agent"

    @property
    def default_model_type(self):
        return None

    def _get_tools(self):
        return []


class _SubtaskRunner:
    def __init__(self, *, exc: Exception | None = None):
        self.logger = logging.getLogger(__name__)
        self._exc = exc

    def run(self, task: str, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return f"ok:{task}"


class _MemoryBackedSubtaskRunner(_SubtaskRunner):
    def __init__(self, *, exc: Exception | None = None):
        super().__init__(exc=exc)
        self.memory = type("_Memory", (), {"steps": ["prompt", "tool", "final"]})()


class _RecordingCheckpointCoordinator:
    def __init__(self):
        self.success_memory_steps = None
        self.interrupted_memory_steps = None

    def prepare_worker_call(
        self, agent_name, input_hash, task_input, *, runtime_agent=None
    ):
        return type(
            "_Preparation",
            (),
            {"call_index": 0, "should_execute": True, "cached_result": None},
        )()

    def restore_worker(self, runtime_agent, agent_name, call_index):
        return False

    def record_worker_success(
        self, agent_name, call_index, input_hash, task_input, result, memory_steps
    ):
        self.success_memory_steps = memory_steps

    def record_worker_failure(
        self, agent_name, call_index, input_hash, task_input, error, memory_steps
    ):
        raise AssertionError("worker failure should not be recorded")

    def record_worker_interrupted(
        self, agent_name, call_index, input_hash, task_input, memory_steps
    ):
        self.interrupted_memory_steps = memory_steps


class TestTaskLifecycleHooks(unittest.TestCase):
    """Verify HookEvent.TASK_CREATED / SUBAGENT_START / SUBAGENT_STOP dispatch."""

    def setUp(self):
        _reset_singletons()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)
        self.project_root = Path(self.temp_dir.name)
        self.runtime_root = self.project_root / ".agentloom"
        self.application_id = "test-app"
        os.environ["AGENTLOOM_RUNTIME_ROOT"] = str(self.runtime_root)
        os.environ["APPLICATION_ID"] = self.application_id
        os.environ["TASK_ID"] = "test-task"

        # Copy skill into temp dir so hook scripts run with temp dir as cwd
        # while runtime files stay under the canonical runtime home.
        source_skill = AGENT_LOOM_ROOT / "skills" / "agent-recall-with-files"
        self.skill_dir = Path(self.temp_dir.name) / "agent-recall-with-files"
        shutil.copytree(source_skill, self.skill_dir)

        self.hook_manager = HookManager()
        self.skills_manager = SkillsManager(
            logger=logging.getLogger(__name__),
            hook_manager=self.hook_manager,
        )
        self.skills_manager.load_skill_metadata(
            str(self.skill_dir / "SKILL.md")
        )

        set_current_skills_manager(self.skills_manager)
        set_current_hook_manager(self.hook_manager)

    def tearDown(self):
        clear_current_skills_manager()
        clear_current_hook_manager()
        os.environ.pop("AGENTLOOM_RUNTIME_ROOT", None)
        os.environ.pop("APPLICATION_ID", None)
        os.environ.pop("TASK_ID", None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def _agent_root(self, agent_name: str) -> Path:
        return self.runtime_root / "workspaces" / "agents" / self.application_id / agent_name

    def _task_workspace(self, agent_name: str, task_id: str) -> Path:
        return self._agent_root(agent_name) / "tasks" / task_id

    def test_task_start_event_bootstraps_runtime_and_cleans_legacy_state(self):
        """TaskCreated should create a canonical task workspace and clean legacy root files."""
        self.skills_manager.get_skill_content("agent-recall-with-files")

        # Create legacy files inside the skill dir (hooks run with cwd=skill_dir).
        sd = self.skill_dir
        legacy_planning_dir = sd / ".planning" / "legacy_agent"
        legacy_planning_dir.mkdir(parents=True, exist_ok=True)
        (legacy_planning_dir / "task_plan.md").write_text("# Legacy Plan\n", encoding="utf-8")
        (sd / "task_plan.md").write_text("# Legacy Root Task Plan\n", encoding="utf-8")
        (sd / "findings.md").write_text("# Legacy Root Findings\n", encoding="utf-8")
        (sd / "progress.md").write_text("# Legacy Root Progress\n", encoding="utf-8")

        with task_context("lifecycle-init"):
            result = self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "lifecycle-init",
                    "cwd": os.getcwd(),
                    "task_text": "test lifecycle init",
                    "agent_name": "test_agent",
                },
            )

        runtime_dir = self._task_workspace("test_agent", "lifecycle-init")
        self.assertTrue(runtime_dir.exists(), f"Expected {runtime_dir} to exist")
        self.assertTrue((self._agent_root("test_agent") / "insights.md").exists())
        self.assertTrue((runtime_dir / "trace.md").exists())
        self.assertTrue((runtime_dir / "context.md").exists())
        self.assertFalse((runtime_dir / "task_plan.md").exists())
        self.assertFalse((sd / ".planning").exists())
        self.assertFalse((sd / "task_plan.md").exists())
        self.assertFalse((sd / "findings.md").exists())
        self.assertFalse((sd / "progress.md").exists())
        self.assertIn(str(runtime_dir), result.agent_context or "")

    def test_task_start_uses_default_agent_name(self):
        """TaskCreated with no agent_name falls back to 'default'."""
        self.skills_manager.get_skill_content("agent-recall-with-files")

        with task_context("lifecycle-default"):
            self.hook_manager.trigger_hooks(
                HookEvent.TASK_CREATED,
                tool_name="",
                tool_input={
                    "task_id": "lifecycle-default",
                    "cwd": os.getcwd(),
                    "task_text": "test default agent",
                },
            )

        runtime_dir = self._task_workspace("default", "lifecycle-default")
        self.assertTrue(runtime_dir.exists(), f"Expected {runtime_dir} to exist")
        self.assertTrue((runtime_dir / "trace.md").exists())
        self.assertTrue((self._agent_root("default") / "insights.md").exists())
        self.assertTrue((runtime_dir / "context.md").exists())

    def test_subtask_events_return_guidance_without_mutating_runtime_files(self):
        """Subtask lifecycle hooks should not auto-write markdown after bootstrap."""
        self.skills_manager.get_skill_content("agent-recall-with-files")

        agent_name = "worker_agent"
        runtime_dir = self._task_workspace(agent_name, "lifecycle-subtask")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "trace.md").write_text("# Trace\n\nexisting-trace\n", encoding="utf-8")
        insights_file = self._agent_root(agent_name) / "insights.md"
        insights_file.write_text("# Insights\n\nexisting-insight\n", encoding="utf-8")

        with task_context("lifecycle-subtask"):
            set_current_agent_name(agent_name)
            try:
                start_result = self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_START,
                    tool_name="step0_research",
                    tool_input={"agent_name": agent_name, "task_id": "lifecycle-subtask"},
                )
                finish_result = self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_STOP,
                    tool_name="step0_research",
                    tool_input={"agent_name": agent_name, "task_id": "lifecycle-subtask"},
                )
            finally:
                clear_current_agent_name()

        trace_content = (runtime_dir / "trace.md").read_text(encoding="utf-8")
        insights_content = insights_file.read_text(encoding="utf-8")
        self.assertEqual(trace_content, "# Trace\n\nexisting-trace\n")
        self.assertEqual(insights_content, "# Insights\n\nexisting-insight\n")
        self.assertIn("trace.md", start_result.user_message or "")
        self.assertIn("step0_research", start_result.user_message or "")
        self.assertIsNone(finish_result.user_message)
        self.assertIn("trace.md", finish_result.agent_context or "")
        self.assertIn("step0_research", finish_result.agent_context or "")

    def test_subtask_failure_returns_manual_recording_guidance(self):
        self.skills_manager.get_skill_content("agent-recall-with-files")

        agent_name = "worker_agent"
        runtime_dir = self._task_workspace(agent_name, "lifecycle-subtask-fail")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        insights_file = self._agent_root(agent_name) / "insights.md"
        insights_file.write_text("# Insights\n\n", encoding="utf-8")
        (runtime_dir / "trace.md").write_text("# Trace\n\n", encoding="utf-8")

        with task_context("lifecycle-subtask-fail"):
            set_current_agent_name(agent_name)
            try:
                self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_START,
                    tool_name="step0_research",
                    tool_input={"agent_name": agent_name, "task_id": "lifecycle-subtask-fail"},
                )
                result = self.hook_manager.trigger_hooks(
                    HookEvent.SUBAGENT_STOP,
                    tool_name="step0_research",
                    tool_input={
                        "agent_name": agent_name,
                        "task_id": "lifecycle-subtask-fail",
                        "success": False,
                        "error": "boom-subtask",
                    },
                )
            finally:
                clear_current_agent_name()

        trace_content = (runtime_dir / "trace.md").read_text(encoding="utf-8")
        insights_content = insights_file.read_text(encoding="utf-8")
        self.assertEqual(trace_content, "# Trace\n\n")
        self.assertEqual(insights_content, "# Insights\n\n")
        self.assertIsNone(result.user_message)
        self.assertIn("trace.md", result.agent_context or "")
        self.assertIn("insights.md", result.agent_context or "")
        self.assertIn("boom-subtask", result.agent_context or "")

    def test_hook_event_enum_has_lifecycle_values(self):
        """Verify HookEvent includes all lifecycle events used by planning runtime."""
        self.assertEqual(HookEvent.TASK_CREATED.value, "TaskCreated")
        self.assertEqual(HookEvent.TASK_COMPLETED.value, "TaskCompleted")
        self.assertEqual(HookEvent.STOP_FAILURE.value, "StopFailure")
        self.assertEqual(HookEvent.SUBAGENT_START.value, "SubagentStart")
        self.assertEqual(HookEvent.SUBAGENT_STOP.value, "SubagentStop")

    def test_shell_hook_receives_structured_context_json(self):
        skill_dir = Path(self.temp_dir.name) / "context-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: context-skill
                description: Exposes hook context to shell
                hooks:
                  SubagentStop:
                    - matcher: "*"
                      hooks:
                        - type: command
                          command: >-
                            python3 -c "import json, os; ctx = json.loads(os.environ['HOOK_CONTEXT_JSON']); print(json.dumps({'reason': ctx['tool_input']['error']}))"
                ---
                # Context Skill
                """
            ),
            encoding="utf-8",
        )

        self.skills_manager.load_skill_metadata(str(skill_dir / "skill.md"))
        self.skills_manager.get_skill_content("context-skill")

        result = self.hook_manager.trigger_hooks(
            HookEvent.SUBAGENT_STOP,
            tool_name="step0_research",
            tool_input={
                "agent_name": "worker_agent",
                "task_id": "lifecycle-json",
                "success": False,
                "error": "boom-json",
            },
        )

        self.assertEqual(result.reason, "boom-json")

    def test_shell_hook_runs_with_skill_dir_as_cwd(self):
        skill_dir = Path(self.temp_dir.name) / "context-skill-cwd"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: context-skill-cwd
                description: Verifies hook runs with skill dir as cwd
                hooks:
                  SubagentStop:
                    - matcher: "*"
                      hooks:
                        - type: command
                          command: >-
                            python3 -c "import json, os; print(json.dumps({'reason': os.getcwd()}))"
                ---
                # Context Skill CWD
                """
            ),
            encoding="utf-8",
        )

        self.skills_manager.load_skill_metadata(str(skill_dir / "skill.md"))
        self.skills_manager.get_skill_content("context-skill-cwd")

        result = self.hook_manager.trigger_hooks(
            HookEvent.SUBAGENT_STOP,
            tool_name="step0_research",
            tool_input={
                "agent_name": "worker_agent",
                "task_id": "lifecycle-skill-cwd",
                "success": True,
            },
        )

        self.assertEqual(Path(result.reason).resolve(), skill_dir.resolve())

    def test_stop_hook_allows_without_plan_validation(self):
        """Stop hook should always allow and never read task_plan state."""
        self.skills_manager.get_skill_content("agent-recall-with-files")

        agent_name = "checker_agent"
        runtime_dir = self._task_workspace(agent_name, "test-task")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "trace.md").write_text("# Trace\n\nsomething\n", encoding="utf-8")
        (self.skill_dir / "task_plan.md").write_text("# invalid legacy plan\n", encoding="utf-8")

        # Set agent name in context so shell hook env injection picks it up
        set_current_agent_name(agent_name)
        try:
            stop_result = self.hook_manager.trigger_hooks(
                HookEvent.STOP,
                tool_name="",
                tool_input={"final_answer": "done"},
            )
        finally:
            clear_current_agent_name()

        self.assertEqual(stop_result.decision, "allow")
        self.assertIn("allow", (stop_result.reason or "").lower())

    def test_task_complete_returns_recording_guidance_only(self):
        self.skills_manager.get_skill_content("agent-recall-with-files")

        agent_name = "checker_agent"
        runtime_dir = self._task_workspace(agent_name, "complete-task")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        insights_file = self._agent_root(agent_name) / "insights.md"
        insights_file.write_text("# Insights\n\n", encoding="utf-8")
        (runtime_dir / "trace.md").write_text("# Trace\n\n", encoding="utf-8")

        set_current_agent_name(agent_name)
        try:
            result = self.hook_manager.trigger_hooks(
                HookEvent.TASK_COMPLETED,
                tool_name="task",
                tool_input={"agent_name": agent_name, "task_id": "complete-task"},
            )
        finally:
            clear_current_agent_name()

        self.assertEqual((runtime_dir / "trace.md").read_text(encoding="utf-8"), "# Trace\n\n")
        self.assertEqual(insights_file.read_text(encoding="utf-8"), "# Insights\n\n")
        self.assertIn("trace.md", result.user_message or "")
        self.assertIn("complete-task", result.user_message or "")

    def test_base_agent_task_lifecycle_flushes_user_messages_immediately(self):
        skill_dir = Path(self.temp_dir.name) / "lifecycle-flush-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: lifecycle-flush-skill
                description: Emits lifecycle user_message payloads for flush testing
                hooks:
                  TaskCreated:
                    - hooks:
                        - type: command
                          command: >-
                            python3 -c "import json; print(json.dumps({'user_message': 'custom-task-start'}))"
                  TaskCompleted:
                    - hooks:
                        - type: command
                          command: >-
                            python3 -c "import json; print(json.dumps({'user_message': 'custom-task-complete'}))"
                  StopFailure:
                    - hooks:
                        - type: command
                          command: >-
                            python3 -c "import json; print(json.dumps({'user_message': 'custom-task-fail'}))"
                ---
                # Lifecycle Flush Skill
                """
            ),
            encoding="utf-8",
        )

        self.skills_manager.load_skill_metadata(str(skill_dir / "skill.md"))
        self.skills_manager.get_skill_content("lifecycle-flush-skill")

        delivered: list[str] = []
        self.hook_manager.set_user_message_sink(delivered.append)
        agent = _LifecycleTestAgent(model=object(), logger=logging.getLogger(__name__))
        agent._hook_manager = self.hook_manager

        with task_context("task-flush"):
            agent._emit_task_start(object(), "do work")
            self.assertTrue(any("custom-task-start" in message for message in delivered))
            self.assertEqual(self.hook_manager.consume_pending_user_messages(), [])

            agent._emit_task_lifecycle_event(HookEvent.TASK_COMPLETED, "do work", result="ok")
            self.assertTrue(any("custom-task-complete" in message for message in delivered))
            self.assertEqual(self.hook_manager.consume_pending_user_messages(), [])

            agent._emit_task_lifecycle_event(HookEvent.STOP_FAILURE, "do work", error=RuntimeError("boom-task"))
            self.assertTrue(any("custom-task-fail" in message for message in delivered))
            self.assertEqual(self.hook_manager.consume_pending_user_messages(), [])

    def test_subtask_lifecycle_flushes_user_messages_immediately(self):
        skill_dir = Path(self.temp_dir.name) / "subtask-flush-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: subtask-flush-skill
                description: Emits subtask user_message payloads for flush testing
                hooks:
                  SubagentStart:
                    - matcher: "*"
                      hooks:
                        - type: command
                          command: >-
                            python3 -c "import json; print(json.dumps({'user_message': 'custom-subtask-start'}))"
                  SubagentStop:
                    - matcher: "*"
                      hooks:
                        - type: command
                          command: >-
                            python3 -c "import json; print(json.dumps({'user_message': 'custom-subtask-finish'}))"
                ---
                # Subtask Flush Skill
                """
            ),
            encoding="utf-8",
        )

        self.skills_manager.load_skill_metadata(str(skill_dir / "skill.md"))
        self.skills_manager.get_skill_content("subtask-flush-skill")

        delivered: list[str] = []
        self.hook_manager.set_user_message_sink(delivered.append)

        wrapped = SubTaskTrackedAgent(_SubtaskRunner(), "worker_agent")
        wrapped.run("do work")
        self.assertTrue(any("custom-subtask-start" in message for message in delivered))
        self.assertTrue(any("custom-subtask-finish" in message for message in delivered))
        self.assertEqual(self.hook_manager.consume_pending_user_messages(), [])

    def test_subtask_checkpoint_records_runtime_memory_before_outer_fallback(self):
        coord = _RecordingCheckpointCoordinator()
        wrapped = SubTaskTrackedAgent(_MemoryBackedSubtaskRunner(), "worker_agent")

        with patch(
            "src.lib.checkpoint.coordinator.CheckpointCoordinator.current",
            return_value=coord,
        ):
            result = wrapped.run("do work")

        self.assertEqual(result, "ok:do work")
        self.assertEqual(coord.success_memory_steps, ["prompt", "tool", "final"])

    def test_subtask_cached_falsey_result_does_not_execute_runtime(self):
        class _CachedCoordinator:
            def __init__(self, result):
                self.result = result

            def prepare_worker_call(self, *args, **kwargs):
                return type(
                    "_Preparation",
                    (),
                    {
                        "call_index": 0,
                        "should_execute": False,
                        "cached_result": self.result,
                    },
                )()

        class _MustNotRun(_SubtaskRunner):
            def run(self, task: str, *args, **kwargs):
                raise AssertionError("cached worker runtime executed")

        for cached_result in ("", None):
            with self.subTest(cached_result=cached_result):
                wrapped = SubTaskTrackedAgent(_MustNotRun(), "worker_agent")
                with patch(
                    "src.lib.checkpoint.coordinator.CheckpointCoordinator.current",
                    return_value=_CachedCoordinator(cached_result),
                ):
                    self.assertEqual(wrapped.run("do work"), cached_result)

    def test_subtask_checkpoint_records_interrupted_runtime_memory(self):
        coord = _RecordingCheckpointCoordinator()
        wrapped = SubTaskTrackedAgent(
            _MemoryBackedSubtaskRunner(exc=KeyboardInterrupt()),
            "worker_agent",
        )

        with patch(
            "src.lib.checkpoint.coordinator.CheckpointCoordinator.current",
            return_value=coord,
        ):
            with self.assertRaises(KeyboardInterrupt):
                wrapped.run("do work")

        self.assertEqual(coord.interrupted_memory_steps, ["prompt", "tool", "final"])

    def test_task_start_preserves_existing_insights(self):
        """TaskStart must preserve insights.md that has real content from prior sessions."""
        self.skills_manager.get_skill_content("agent-recall-with-files")

        agent_name = "persist_agent"
        agent_root = self._agent_root(agent_name)
        prior_task_dir = self._task_workspace(agent_name, "prior-task")
        prior_task_dir.mkdir(parents=True, exist_ok=True)
        prior_insights = (
            "# Insights\n\n## Log\n"
            "- [2026-03-12] [pitfall] Do not call CanIf_Init from task context.\n"
            "- [2026-03-12] [decision] Use CanIf_SetControllerMode instead.\n"
        )
        (agent_root / "insights.md").write_text(prior_insights, encoding="utf-8")
        (prior_task_dir / "trace.md").write_text("# old trace\n", encoding="utf-8")

        with task_context("lifecycle-persist"):
            set_current_agent_name(agent_name)
            try:
                result = self.hook_manager.trigger_hooks(
                    HookEvent.TASK_CREATED,
                    tool_name="",
                    tool_input={
                        "task_id": "lifecycle-persist",
                        "cwd": os.getcwd(),
                        "task_text": "test persistence",
                        "agent_name": agent_name,
                    },
                )
            finally:
                clear_current_agent_name()

        # insights.md should be preserved with prior content.
        runtime_dir = self._task_workspace(agent_name, "lifecycle-persist")
        insights_content = (agent_root / "insights.md").read_text(encoding="utf-8")
        self.assertIn("CanIf_Init", insights_content)
        self.assertIn("[pitfall]", insights_content)

        # trace.md should be recreated from template (old content gone).
        trace_content = (runtime_dir / "trace.md").read_text(encoding="utf-8")
        self.assertNotIn("old trace", trace_content)
        self.assertIn("Trace", trace_content)

        # context.md should be freshly created.
        self.assertTrue((runtime_dir / "context.md").exists())

        # Agent context should mention prior insights.
        self.assertIn("previous tasks", result.agent_context or "")

    def test_task_start_preserves_trace_and_context_when_resuming_same_task(self):
        """TaskStart keeps task-scoped state when the same task id resumes."""
        self.skills_manager.get_skill_content("agent-recall-with-files")

        agent_name = "reset_agent"
        runtime_dir = self._task_workspace(agent_name, "lifecycle-reset")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "trace.md").write_text("# Old Trace\n\nstale entry\n", encoding="utf-8")
        (runtime_dir / "context.md").write_text("# Old Context\n\nstale goal\n", encoding="utf-8")

        with task_context("lifecycle-reset"):
            set_current_agent_name(agent_name)
            try:
                self.hook_manager.trigger_hooks(
                    HookEvent.TASK_CREATED,
                    tool_name="",
                    tool_input={
                        "task_id": "lifecycle-reset",
                        "cwd": os.getcwd(),
                        "task_text": "test reset",
                        "agent_name": agent_name,
                    },
                )
            finally:
                clear_current_agent_name()

        trace_content = (runtime_dir / "trace.md").read_text(encoding="utf-8")
        context_content = (runtime_dir / "context.md").read_text(encoding="utf-8")
        self.assertIn("stale entry", trace_content)
        self.assertIn("stale goal", context_content)


if __name__ == "__main__":
    unittest.main()
