"""Trial context reaches the actual public Application lifecycle and models."""

import pytest

from agentloom.app import Application
from agentloom.app.harnessing.models import TrialContext, bind_trial_context
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config

from tests.application_test.mixed_runtime_support import finish, model_service, project, tool_messages, write_yaml


def test_trial_uses_own_runtime_without_rewriting_resource_config(tmp_path):
    project_root = tmp_path / "project"
    definition = project(project_root, "http://unused.invalid", supervisor="smolagents", worker="smolagents")
    source = project_root / "config/system.yaml"
    original = source.read_bytes()
    context = TrialContext("experiment", "trial", "candidate", "task", tmp_path / "trial")
    with bind_config(load_project_config(project_root)), bind_trial_context(context):
        with Application.open(definition.parent.parent, file_logging=False) as app:
            assert app.run.run_dir.is_relative_to(context.runtime_root)
            assert app.project_config.raw["runtime"]["root_dir"] == str(context.runtime_root)
    assert source.read_bytes() == original
    assert not (project_root / "runtime").exists()


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_trial_freezes_memory_for_root_and_worker_models(tmp_path, runtime, monkeypatch):
    def forbid_live_memory(*args, **kwargs):
        raise AssertionError("Trial attempted to refresh its frozen memory view")

    from agentloom.self_learning.persistence.memory_store import MemoryStore
    monkeypatch.setattr(MemoryStore, "snapshot_for_prompt", forbid_live_memory)

    def program(request):
        visible = str(request["messages"])
        assert "FROZEN-MEMORY-136" in visible
        assert "PRIVATE-SCORE-136" not in visible
        if request["model"] == "worker":
            return finish(request, "WORKER-OK-136")
        if not tool_messages(request):
            return [("child-136", "inspect_note", {"query": "Check the frozen view."})]
        return finish(request, "ROOT-OK-136")

    with model_service(program) as (url, requests):
        project_root = tmp_path / "project"
        definition = project(project_root, url, supervisor=runtime, worker=runtime)
        write_yaml(project_root / "applications/mixed/config/system.yaml", {"self_learning": {"enabled": False}})
        context = TrialContext("experiment", "trial", "candidate", "task", tmp_path / "trial",
                               memory_snapshot="FROZEN-MEMORY-136")
        with bind_config(load_project_config(project_root)), bind_trial_context(context):
            result = execute_app(definition, file_logging=False)
    assert result.output == "ROOT-OK-136"
    assert result.run.run_dir.is_relative_to(context.runtime_root)
    assert any(request["model"] == "worker" for request in requests)
