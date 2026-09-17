from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from agentloom.application.definition import definition_error, load_agent_definition, validate_agent_definition
from agentloom.tui_bridge.bridge import TuiBridge
from agentloom.tui_bridge.catalog import project_catalog
from agentloom.tui_bridge.domain_cli import main as domain_main


def _markdown(path: Path, config: dict, body: str = "Run the declared task.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("```yaml\n" + yaml.safe_dump(config, sort_keys=False) + "```\n\n" + body + "\n")


def _project(root: Path, app_id: str = "markdown", folder: str = "") -> tuple[Path, Path]:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "system.yaml").write_text("{}\n")
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: test\n  test: {model: openai/test}\n  summary: {model: openai/test}\n"
    )
    supervisor = root / "applications" / app_id / "workflows" / folder / "supervisor.md"
    worker = supervisor.parent / "worker_agents/worker.md"
    _markdown(supervisor, {
        "name": "markdown_supervisor", "description": "Coordinate Markdown work",
        "worker_agents": [{"path": "worker.md"}],
        "tools": [{"name": "read_file"}],
        "hooks": {"SessionStart": [{"id": "read-only-check", "command": "touch must-not-exist"}]},
    })
    _markdown(worker, {
        "name": "markdown_worker", "description": "Read the declared task",
        "agent_function_schema": {
            "description": "Work on one task", "inputs": {"task": {"description": "Task", "required": True}},
            "output": {"description": "Evidence"},
        },
    })
    return supervisor, worker


@pytest.mark.parametrize("app_id", ["markdown", "group/suite/markdown"])
@pytest.mark.parametrize("folder", ["", "nested"])
def test_markdown_supervisor_and_worker_are_discovered_through_all_read_only_surfaces(tmp_path, capsys, app_id, folder):
    supervisor, worker = _project(tmp_path, app_id, folder)
    relative = supervisor.relative_to(tmp_path).as_posix()
    bridge = TuiBridge(tmp_path)
    bootstrap = bridge.bootstrap()
    assert len(bootstrap["systems"]) == 1
    system = bootstrap["systems"][0]
    assert system["id"] == relative
    assert system["application_id"] == app_id
    assert system["validation"] == {"valid": True, "errors": []}
    assert bootstrap["applications"][0]["id"] == app_id
    assert bootstrap["applications"][0]["system_count"] == 1
    assert bootstrap["applications"][0]["worker_count"] == 1
    assert bootstrap["agents"][0]["path"] == relative
    assert bootstrap["agents"][0]["workers"][0]["path"] == worker.relative_to(tmp_path).as_posix()

    # Application existence discovery must work even without precomputed systems.
    discovered = project_catalog(tmp_path, [], [])
    assert [app["id"] for app in discovered["applications"]] == [app_id]
    detail = bridge.system_detail(relative)
    assert detail["topology"]["supervisor"]["path"] == relative
    assert detail["definition"]["workflow"] == "Run the declared task."
    application = bridge.dispatch("application.detail", {"application_id": app_id})
    assert application["application"]["health"] == "healthy"
    assert application["agents"][0]["workers"][0]["validation"]["valid"] is True
    assert bridge._validate_run_system({"yaml_path": str(supervisor)}, application_id=app_id, system_id=relative) == relative
    assert domain_main(["--project", str(tmp_path), "application.validate", json.dumps({"application_id": app_id})]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["valid"] is True
    assert validate_agent_definition(tmp_path, str(supervisor), load_agent_definition(supervisor)) == []
    assert not (tmp_path / ".agentloom").exists()
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("target", ["supervisor", "worker"])
@pytest.mark.parametrize("fault", ["duplicate_key", "no_yaml", "removed_field", "missing_workflow"])
def test_markdown_discovery_preserves_shared_definition_diagnostics(tmp_path, capsys, target, fault):
    supervisor, worker = _project(tmp_path, "group/markdown", "nested")
    source = supervisor if target == "supervisor" else worker
    config = load_agent_definition(source)
    config.pop("_yaml_file_path")
    config.pop("workflow")
    if fault == "duplicate_key":
        source.write_text(source.read_text().replace("```\n", "name: duplicate\n```\n", 1))
    elif fault == "no_yaml":
        source.write_text("Markdown without a YAML configuration block.")
    elif fault == "removed_field":
        config["tools_mapping"] = {}
        _markdown(source, config)
    else:
        _markdown(source, config, body="")
    try:
        definition = load_agent_definition(supervisor)
    except ValueError as exc:
        canonical = [definition_error(exc)]
    else:
        canonical = validate_agent_definition(tmp_path, str(supervisor), definition)
    assert canonical
    bridge = TuiBridge(tmp_path)
    system = bridge.bootstrap()["systems"][0]
    assert system["validation"]["valid"] is False
    detail = bridge.dispatch("application.detail", {"application_id": "group/markdown"})
    assert detail["application"]["health"] == "invalid"
    assert any(str(source) in error for error in detail["agents"][0]["validation"]["errors"])
    assert domain_main(["--project", str(tmp_path), "application.validate", '{"application_id":"group/markdown"}']) == 0
    public = json.loads(capsys.readouterr().out)["result"]
    assert public["valid"] is False
    for reason in canonical:
        assert any(reason in message for message in public["errors"])
    assert not (tmp_path / ".agentloom").exists()


def test_markdown_discovery_does_not_promote_workers_or_follow_symlinks(tmp_path):
    supervisor, _ = _project(tmp_path)
    _markdown(tmp_path / "applications/worker_only/workflows/worker_agents/orphan.md", {
        "name": "orphan", "description": "Unreferenced Worker",
    })
    (supervisor.parent / "linked.md").symlink_to(supervisor)
    (supervisor.parent / "linked_dir").symlink_to(supervisor.parent, target_is_directory=True)
    bridge = TuiBridge(tmp_path)
    assert [system["id"] for system in bridge.bootstrap()["systems"]] == [supervisor.relative_to(tmp_path).as_posix()]
    assert [app["id"] for app in project_catalog(tmp_path, [], [])["applications"]] == ["markdown"]


def test_markdown_catalog_and_details_do_not_construct_runtime_or_write_files(tmp_path):
    supervisor, _ = _project(tmp_path, "nested/markdown")
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    program = """
import json, sys
from pathlib import Path
from agentloom.tui_bridge.bridge import TuiBridge
from agentloom.tui_bridge.domain_cli import main
root = Path(sys.argv[1])
bridge = TuiBridge(root)
assert len(bridge.bootstrap()['systems']) == 1
assert bridge.system_detail(sys.argv[2])['definition']['workflow'] == 'Run the declared task.'
assert bridge.dispatch('application.detail', {'application_id':'nested/markdown'})['application']['health'] == 'healthy'
assert main(['--project', str(root), 'application.validate', '{"application_id":"nested/markdown"}']) == 0
for prefix in ('litellm', 'agentloom.runtime.agent', 'agentloom.application.runner', 'agentloom.tools.file_ops', 'agentloom.tools.shell', 'agentloom.tools.search'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert not (root / '.agentloom').exists()
assert not (root / 'must-not-exist').exists()
"""
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), supervisor.relative_to(tmp_path).as_posix()],
        cwd=tmp_path, text=True, capture_output=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    after = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_schedule_accepts_markdown_supervisor_in_temporary_store(tmp_path):
    from agentloom.schedules.store import ScheduleStore

    supervisor, _ = _project(tmp_path, "nested/markdown", "subgroup")
    relative = supervisor.relative_to(tmp_path).as_posix()
    bridge = TuiBridge(tmp_path)
    result = bridge.dispatch("schedule.add", {
        "yaml_path": relative, "name": "Markdown schedule",
        "schedule": {"kind": "once", "at": "2099-01-02T03:04:05Z", "timezone": "UTC"},
    })
    jobs = ScheduleStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == result["job_id"]
    assert jobs[0]["yaml_path"] == relative
    assert bridge.bootstrap()["schedules"]["items"][0]["yaml_path"] == relative
    assert not (tmp_path / ".agentloom/runs").exists()
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("target", ["worker", "invalid", "symlink", "absolute", "parent_traversal"])
def test_schedule_rejects_invalid_or_unsafe_markdown_targets_before_store_write(tmp_path, target):
    from agentloom.tui_bridge.bridge import BridgeError

    supervisor, worker = _project(tmp_path)
    candidate = supervisor.relative_to(tmp_path).as_posix()
    if target == "worker":
        candidate = worker.relative_to(tmp_path).as_posix()
    elif target == "invalid":
        supervisor.write_text("```yaml\nname: duplicate\nname: duplicate\n```\nWork")
    elif target == "symlink":
        link = supervisor.parent / "linked.md"
        link.symlink_to(supervisor)
        candidate = link.relative_to(tmp_path).as_posix()
    elif target == "absolute":
        candidate = str(supervisor)
    else:
        outside = tmp_path.parent / (tmp_path.name + "-outside.md")
        _markdown(outside, {"name": "outside", "description": "Outside definition"})
        candidate = "../" + outside.name
    with pytest.raises(BridgeError) as error:
        TuiBridge(tmp_path).dispatch("schedule.add", {
            "yaml_path": candidate, "name": "Invalid Markdown schedule",
            "schedule": {"kind": "once", "at": "2099-01-02T03:04:05Z", "timezone": "UTC"},
        })
    assert error.value.code == "invalid_params"
    assert not (tmp_path / ".agentloom").exists()
