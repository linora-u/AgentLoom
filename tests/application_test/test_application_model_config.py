"""Application model loading, selection and invocation isolation."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import yaml

from agentloom.app.definition import inspect_application_definition, load_agent_definition
from agentloom.config.config import C, bind_config, bind_model_config, build_effective_agent_config_snapshot, load_project_config


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path


def profile(model, **kwargs):
    return {"model": model, "adapter": "openai_chat", **kwargs}


def agent(root, app, *, runtime="pi", **kwargs):
    return write_yaml(root / f"applications/{app}/workflows/root.yaml", {
        "name": app.replace("/", "_"), "description": "Model configuration test", "task": "Answer",
        "agent_runtime": runtime, "tools": [], "toolsets": [], **kwargs,
    })


def test_application_replaces_whole_profile_and_preserves_global_models(tmp_path):
    write_yaml(tmp_path / "config/model.yaml", {"model": {
        "default_model_type": "fast", "fast": profile("global", api_key="global-key", extra_headers={"X-Global": "1"}),
        "other": profile("other"),
    }})
    write_yaml(tmp_path / "applications/a/config/model.yaml", {"model": {
        "default_model_type": "local", "fast": profile("app"), "local": profile("local"),
    }})
    path = agent(tmp_path, "a")
    base = load_project_config(tmp_path)
    snapshot = build_effective_agent_config_snapshot(load_agent_definition(path), base_config=base)
    assert snapshot.llm.for_type("fast").model == "app"
    assert snapshot.llm.for_type("fast").api_key == ""
    assert snapshot.llm.for_type("fast").extra_headers is None
    assert snapshot.llm.for_type(None).model == "local"
    assert snapshot.llm.for_type("other").model == "other"
    assert base.llm.for_type(None).model == "global"
    assert "local" not in base.llm.models


def test_cross_application_workers_use_own_catalogues(tmp_path):
    write_yaml(tmp_path / "config/model.yaml", {"model": {"default_model_type": "fast", "fast": profile("global")}})
    for app in ("a", "b"):
        write_yaml(tmp_path / f"applications/{app}/config/model.yaml", {"model": {
            "default_model_type": app, app: profile(app), "fast": profile(f"{app}-fast"),
        }})
    child = agent(tmp_path, "b", input_schema={
        "type": "object", "properties": {"query": {"type": "string", "description": "Query"}},
        "required": ["query"], "additionalProperties": False,
    })
    parent = agent(tmp_path, "a", worker_agents=[{"path": "../b/workflows/root.yaml"}])
    inspection = inspect_application_definition(tmp_path, str(parent), load_agent_definition(parent))
    assert not inspection.errors
    assert inspection.snapshots[parent].llm.for_type(None).model == "a"
    assert inspection.snapshots[child].llm.for_type(None).model == "b"
    assert "a" not in inspection.snapshots[child].llm.models


@pytest.mark.parametrize("runtime,smart_summary,needs_summary", [
    ("pi", None, False), ("smolagents", False, False), ("smolagents", True, True),
])
def test_application_only_catalogue_and_conditional_summary(tmp_path, runtime, smart_summary, needs_summary):
    write_yaml(tmp_path / "applications/a/config/model.yaml", {"model": {"default_model_type": "fast", "fast": profile("local")}})
    options = {"runtime_options": {"smart_summary": smart_summary}} if smart_summary is not None else {}
    path = agent(tmp_path, "a", runtime=runtime, **options)
    inspection = inspect_application_definition(tmp_path, str(path), load_agent_definition(path))
    if needs_summary:
        assert any("summary" in error and "required" in error for error in inspection.errors)
    else:
        assert not inspection.errors
    assert inspection.snapshots[path].llm.for_type(None).model == "local"


def test_models_follow_nearest_application_and_refresh_for_new_execution(tmp_path):
    write_yaml(tmp_path / "applications/parent/config/model.yaml", {"model": {"fast": profile("parent")}})
    local = tmp_path / "applications/parent/child/config/model.yaml"
    write_yaml(local, {"model": {"default_model_type": "fast", "fast": profile("before")}})
    path = agent(tmp_path, "parent/child")
    base = load_project_config(tmp_path)
    before = build_effective_agent_config_snapshot(load_agent_definition(path), base_config=base)
    write_yaml(local, {"model": {"default_model_type": "fast", "fast": profile("after")}})
    after = build_effective_agent_config_snapshot(load_agent_definition(path), base_config=base)
    assert before.llm.for_type(None).model == "before"
    assert after.llm.for_type(None).model == "after"


def test_concurrent_agent_model_contexts_do_not_leak(tmp_path):
    from agentloom.config.llm_config import LLMConfig

    base = load_project_config(tmp_path)
    barrier = Barrier(2)
    def read(app):
        catalog = LLMConfig.from_dict({"model": {"default_model_type": "fast", "fast": profile(app), "summary": profile(f"{app}-summary")}})
        with bind_config(base), bind_model_config(catalog):
            barrier.wait(timeout=5)
            assert C.default_model_type == "fast"
            return C.llm.for_type(None).model, C.llm.for_type("summary").model
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, ("a", "b"))) == [("a", "a-summary"), ("b", "b-summary")]
    with bind_config(base):
        assert not C.llm.models


def test_snapshot_instructions_can_select_live_application_models(tmp_path):
    live = agent(tmp_path, "a")
    write_yaml(tmp_path / "applications/a/config/model.yaml", {"model": {"default_model_type": "fast", "fast": profile("live")}})
    snapshot = agent(tmp_path, "a/data/snapshots/old")
    write_yaml(snapshot.parent.parent / "config/model.yaml", {"model": {"default_model_type": "fast", "fast": profile("old")}})
    definition = load_agent_definition(snapshot)
    definition["_model_source_path"] = str(live)
    effective = build_effective_agent_config_snapshot(definition, base_config=load_project_config(tmp_path))
    assert effective.llm.for_type(None).model == "live"


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_direct_agent_creation_uses_own_models_even_inside_another_application(tmp_path, runtime):
    from agentloom.app.factory import YamlConfiguredAgent
    from agentloom.execution.logging import NullLoggerBackend

    snapshots = {}
    definitions = {}
    base = load_project_config(tmp_path)
    for app in ("a", "b"):
        write_yaml(tmp_path / f"applications/{app}/config/model.yaml", {"model": {
            "default_model_type": "fast", "fast": profile(f"{app}-model"),
        }})
        options = {"runtime_options": {"smart_summary": False}} if runtime == "smolagents" else {}
        definitions[app] = load_agent_definition(agent(tmp_path, app, runtime=runtime, **options))
        snapshots[app] = build_effective_agent_config_snapshot(definitions[app], base_config=base)
    with bind_config(base), bind_model_config(snapshots["a"].llm):
        worker = YamlConfiguredAgent(config=definitions["b"], logger=NullLoggerBackend())
        selected = worker._model_selection if runtime == "pi" else worker._model_binding
        assert selected.model_id == "b-model"
        assert C.llm.for_type(None).model == "a-model"
    assert not base.llm.models


def test_old_filename_requires_migration(tmp_path):
    write_yaml(tmp_path / "config/llm.yaml", {"model": {"fast": profile("old")}})
    with pytest.raises(ValueError, match="migrate"):
        load_project_config(tmp_path)
