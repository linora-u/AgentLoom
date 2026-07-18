from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from src.lib.smolagents.skills.skills import SkillsManager

ROOT = Path(__file__).parents[2]
VISUALIZATION_BUNDLE = ROOT / "hooks" / "agent-visualization"
RECALL_BUNDLE = ROOT / "hooks" / "agent-recall-with-files"


def _payload(
    project_root: Path,
    *,
    event: str,
    hook_id: str,
    root_run_id: str = "root-run",
    agent_name: str = "supervisor",
    runtime_agent_path: str = "supervisor",
    tool_name: str = "",
    tool_input: dict | None = None,
    tool_response: object = None,
    step_number: int = 0,
) -> dict:
    return {
        "schema_version": 1,
        "hook_id": hook_id,
        "hook_event_name": event,
        "local_run_id": "local-run",
        "root_run_id": root_run_id,
        "agent_name": agent_name,
        "runtime_agent_path": runtime_agent_path,
        "task_id": "task-1",
        "sub_task_id": "",
        "step_number": step_number,
        "project_root": str(project_root),
        "cwd": str(project_root),
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "tool_response": tool_response,
        "tool_inputs_schema": {},
    }


def _invoke_bundle_script(
    bundle: Path,
    script_name: str,
    payload: dict,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Poison the removed transport to prove scripts consume stdin only.
    env["HOOK_CONTEXT_JSON"] = '{"schema_version":0,"agent_name":"wrong"}'
    return subprocess.run(
        [sys.executable, str(bundle / "scripts" / script_name)],
        cwd=bundle,
        env=env,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=10,
        check=check,
    )


def _run_bundle_script(bundle: Path, script_name: str, payload: dict) -> dict:
    completed = _invoke_bundle_script(bundle, script_name, payload)
    return json.loads(completed.stdout)


def _viz_path(
    project_root: Path,
    *,
    root_run_id: str = "root-run",
    root_agent: str = "supervisor",
) -> Path:
    return (
        project_root
        / ".runtime"
        / root_agent
        / root_run_id
        / "visualization.json"
    )


def test_visualization_is_a_hook_bundle_not_a_skill() -> None:
    assert not (ROOT / "skills" / "agent-visualization" / "SKILL.md").exists()
    manifest = yaml.safe_load((VISUALIZATION_BUNDLE / "HOOK.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "agent-visualization"
    assert set(manifest["hooks"]) == {
        "TaskCreated",
        "TaskCompleted",
        "StopFailure",
        "SubagentStart",
        "SubagentStop",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
    }


def test_recall_skill_and_hook_bundle_are_independent() -> None:
    manager = SkillsManager()
    skill = manager.load_skill_metadata(
        str(ROOT / "skills" / "agent-recall-with-files" / "SKILL.md")
    )
    manifest = yaml.safe_load((RECALL_BUNDLE / "HOOK.yaml").read_text(encoding="utf-8"))

    assert skill.metadata.name == "agent-recall-with-files"
    assert not hasattr(skill.metadata, "hooks")
    assert manifest["name"] == skill.metadata.name
    assert not list((ROOT / "skills" / "agent-recall-with-files" / "scripts").glob("*.py"))
    assert {
        path.name
        for path in (ROOT / "skills" / "agent-recall-with-files" / "templates").glob("*.md")
    } == {"context.md", "insights.md", "trace.md"}


def test_system_config_enables_visualization_bundle_only() -> None:
    system = yaml.safe_load((ROOT / "config" / "system.yaml").read_text(encoding="utf-8"))

    assert system["hooks"]["bundles"] == {
        "agent-visualization": {"path": "hooks/agent-visualization"}
    }
    assert system.get("skills") is None


def test_visualization_scripts_read_versioned_stdin_and_write_timeline(
    tmp_path: Path,
) -> None:
    started = _run_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_task_start.py",
        _payload(
            tmp_path,
            event="TaskCreated",
            hook_id="agent-visualization.task-created",
            tool_input={"task_text": "Implement independent Hook Bundles"},
        ),
    )
    assert started["decision"] == "allow"

    observed = _run_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_pre_tool_use.py",
        _payload(
            tmp_path,
            event="PreToolUse",
            hook_id="agent-visualization.pre-tool-use",
            tool_name="read_file",
            tool_input={"file_path": "README.md"},
            step_number=1,
        ),
    )
    assert observed["decision"] == "allow"

    data = json.loads(_viz_path(tmp_path).read_text(encoding="utf-8"))
    assert [event["event_type"] for event in data["timeline"]] == ["start", "tool_call"]
    assert data["timeline"][-1]["tool_name"] == "read_file"


def test_visualization_same_root_concurrent_events_do_not_lose_updates(
    tmp_path: Path,
) -> None:
    _run_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_task_start.py",
        _payload(
            tmp_path,
            event="TaskCreated",
            hook_id="agent-visualization.task-created",
        ),
    )

    event_count = 24

    def emit(index: int) -> dict:
        return _run_bundle_script(
            VISUALIZATION_BUNDLE,
            "on_pre_tool_use.py",
            _payload(
                tmp_path,
                event="PreToolUse",
                hook_id="agent-visualization.pre-tool-use",
                tool_name=f"concurrent_tool_{index}",
                tool_input={"marker": index},
            ),
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(emit, range(event_count)))

    assert all(result["decision"] == "allow" for result in results)
    data = json.loads(_viz_path(tmp_path).read_text(encoding="utf-8"))
    timeline = data["timeline"]
    assert len(timeline) == event_count + 1
    assert [event["step"] for event in timeline] == list(range(1, event_count + 2))
    assert {
        event["tool_name"]
        for event in timeline
        if event["event_type"] == "tool_call"
    } == {f"concurrent_tool_{index}" for index in range(event_count)}


def test_visualization_same_agent_concurrent_root_runs_are_isolated(
    tmp_path: Path,
) -> None:
    root_ids = ("root-a", "root-b")
    for root_run_id in root_ids:
        _run_bundle_script(
            VISUALIZATION_BUNDLE,
            "on_task_start.py",
            _payload(
                tmp_path,
                event="TaskCreated",
                hook_id="agent-visualization.task-created",
                root_run_id=root_run_id,
                tool_input={"task_text": root_run_id},
            ),
        )

    def emit(root_run_id: str, index: int) -> dict:
        return _run_bundle_script(
            VISUALIZATION_BUNDLE,
            "on_pre_tool_use.py",
            _payload(
                tmp_path,
                event="PreToolUse",
                hook_id="agent-visualization.pre-tool-use",
                root_run_id=root_run_id,
                tool_name=f"{root_run_id}-tool-{index}",
            ),
        )

    invocations = [
        (root_run_id, index)
        for root_run_id in root_ids
        for index in range(12)
    ]
    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(lambda args: emit(*args), invocations))

    for root_run_id in root_ids:
        data = json.loads(
            _viz_path(tmp_path, root_run_id=root_run_id).read_text(encoding="utf-8")
        )
        tool_names = {
            event["tool_name"]
            for event in data["timeline"]
            if event["event_type"] == "tool_call"
        }
        assert tool_names == {
            f"{root_run_id}-tool-{index}" for index in range(12)
        }
        assert all(
            name.startswith(root_run_id)
            for name in tool_names
        )


def test_visualization_encodes_non_path_safe_runtime_identities(
    tmp_path: Path,
) -> None:
    result = _run_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_task_start.py",
        _payload(
            tmp_path,
            event="TaskCreated",
            hook_id="agent-visualization.task-created",
            root_run_id="run with spaces/and unicode-运行",
            agent_name="Agent With Spaces",
            runtime_agent_path="Agent With Spaces",
        ),
    )

    path = Path(result["telemetry"]["viz_file"])
    assert path.is_file()
    assert path.is_relative_to(tmp_path / ".runtime")


def test_visualization_workers_resolve_the_root_run_without_directory_scanning(
    tmp_path: Path,
) -> None:
    _run_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_task_start.py",
        _payload(
            tmp_path,
            event="TaskCreated",
            hook_id="agent-visualization.task-created",
        ),
    )
    decoy = _viz_path(tmp_path, root_run_id="other-root", root_agent="decoy")
    decoy.parent.mkdir(parents=True)
    decoy.write_text('{"sentinel": true}\n', encoding="utf-8")

    for worker_name, runtime_path in (
        ("worker", "supervisor/worker"),
        ("nested", "supervisor/worker/nested"),
    ):
        _run_bundle_script(
            VISUALIZATION_BUNDLE,
            "on_subtask_start.py",
            _payload(
                tmp_path,
                event="SubagentStart",
                hook_id="agent-visualization.subagent-start",
                agent_name=worker_name,
                runtime_agent_path=runtime_path,
                tool_input={"agent_name": worker_name},
            ),
        )
        _run_bundle_script(
            VISUALIZATION_BUNDLE,
            "on_pre_tool_use.py",
            _payload(
                tmp_path,
                event="PreToolUse",
                hook_id="agent-visualization.pre-tool-use",
                agent_name=worker_name,
                runtime_agent_path=runtime_path,
                tool_name=f"{worker_name}_tool",
            ),
        )

    data = json.loads(_viz_path(tmp_path).read_text(encoding="utf-8"))
    assert {agent["name"] for agent in data["config"]["agents"]} == {
        "supervisor",
        "worker",
        "nested",
    }
    tool_events = {
        event["tool_name"]: event["agent_name"]
        for event in data["timeline"]
        if event["event_type"] == "tool_call"
    }
    assert tool_events == {"worker_tool": "worker", "nested_tool": "nested"}
    assert not _viz_path(tmp_path, root_agent="worker").exists()
    assert not _viz_path(tmp_path, root_agent="nested").exists()
    assert decoy.read_text(encoding="utf-8") == '{"sentinel": true}\n'


def test_visualization_pre_tool_failure_is_fail_closed(tmp_path: Path) -> None:
    payload = _payload(
        tmp_path,
        event="PreToolUse",
        hook_id="agent-visualization.pre-tool-use",
        tool_name="read_file",
    )
    payload["schema_version"] = 2

    completed = _invoke_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_pre_tool_use.py",
        payload,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "schema_version" in completed.stderr


def test_visualization_observer_failure_is_fail_open(tmp_path: Path) -> None:
    payload = _payload(
        tmp_path,
        event="PostToolUse",
        hook_id="agent-visualization.post-tool-use",
        tool_name="read_file",
        tool_response={"result": "done"},
    )
    payload["schema_version"] = 2

    completed = _invoke_bundle_script(
        VISUALIZATION_BUNDLE,
        "on_post_tool_use.py",
        payload,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"decision": "allow"}
    assert "schema_version" in completed.stderr


def test_recall_bundle_bootstraps_from_versioned_stdin(tmp_path: Path) -> None:
    result = _run_bundle_script(
        RECALL_BUNDLE,
        "on_task_start.py",
        _payload(
            tmp_path,
            event="TaskCreated",
            hook_id="agent-recall-with-files.task-created",
        ),
    )

    assert result["decision"] == "allow"
    runtime_dir = tmp_path / ".runtime" / "supervisor"
    assert (runtime_dir / "context.md").is_file()
    assert (runtime_dir / "trace.md").is_file()
    assert (runtime_dir / "insights.md").is_file()


def test_recall_pre_tool_rejects_an_unknown_stdin_schema(tmp_path: Path) -> None:
    payload = _payload(
        tmp_path,
        event="PreToolUse",
        hook_id="agent-recall-with-files.pre-tool-use",
        tool_name="read_file",
    )
    payload["schema_version"] = 2

    completed = subprocess.run(
        [sys.executable, str(RECALL_BUNDLE / "scripts" / "on_pre_tool_use.py")],
        cwd=RECALL_BUNDLE,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode != 0
    assert "schema_version" in completed.stderr
