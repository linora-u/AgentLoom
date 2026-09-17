from pathlib import Path

import pytest

from src.lib.smolagents.agent.agent_validation import AgentConfigNormalizer
from src.tui_bridge.definition import load_agent_definition, validate_agent_definition


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


BASE = "name: demo\ndescription: Demo\nworkflow: Run the task.\n"
SCHEMA = """agent_function_schema:
  description: Work on one task.
  inputs:
    task:
      description: The task.
      required: true
  output:
    description: The evidence.
"""


@pytest.mark.parametrize("suffix", [".yaml", ".md"])
@pytest.mark.parametrize(
    "body,key",
    [
        (BASE + "name: overwritten\n", "name"),
        (BASE + "hooks:\n  PreToolUse: []\n  PreToolUse: []\n", "PreToolUse"),
        (BASE + "defaults: &d {timeout: 1, timeout: 2}\noptions: {<<: *d}\n", "timeout"),
    ],
)
def test_duplicate_keys_rejected_with_source(tmp_path, suffix, body, key):
    path = write(tmp_path / f"agent{suffix}", f"```yaml\n{body}```\nWorkflow" if suffix == ".md" else body)
    with pytest.raises(ValueError, match=f"Duplicate YAML mapping key: {key!r}"):
        load_agent_definition(path)


def test_merge_defaults_and_markdown_workflow_are_preserved(tmp_path):
    path = write(
        tmp_path / "agent.md",
        """```yaml
name: demo
description: Demo
defaults: &defaults {timeout: 2, retries: 3}
options: {<<: *defaults, timeout: 4}
workflow: discarded
```

The actual Markdown workflow.
""",
    )
    parsed = load_agent_definition(path)
    assert parsed["options"] == {"timeout": 4, "retries": 3}
    assert parsed["workflow"] == "The actual Markdown workflow."


def test_worker_dot_paths_are_relative_to_definition_source(tmp_path):
    worker_folder = tmp_path / "applications/group/demo/workflows/worker_agents"
    assert (
        AgentConfigNormalizer.resolve_worker_agent_config_path(
            "./other/worker.md",
            worker_folder,
            agent_root=tmp_path,
        )
        == worker_folder.parent / "other/worker.md"
    )
    assert (
        AgentConfigNormalizer.resolve_worker_agent_config_path(
            "applications/shared/workflows/worker.md",
            worker_folder,
            agent_root=tmp_path,
        )
        == tmp_path / "applications/shared/workflows/worker.md"
    )


def test_recursive_worker_topology_is_rejected(tmp_path):
    root = write(
        tmp_path / "applications/group/demo/workflows/root.yaml", BASE + "worker_agents:\n  - path: worker.yaml\n"
    )
    write(root.parent / "worker_agents/worker.yaml", BASE + SCHEMA + f"worker_agents:\n  - path: {root}\n")
    errors = validate_agent_definition(
        tmp_path,
        str(root.relative_to(tmp_path)),
        load_agent_definition(root),
        catalog=("test", {"test": {"model": "openai/test"}}),
    )
    assert any("cycle" in error.lower() and "root.yaml" in error for error in errors), errors


def project_config(root):
    from src.lib.config.config import load_project_config

    write(
        root / "config/system.yaml",
        'todo: {mode: auto}\nsmart_summary: "true"\ncontext_engine: {min_chars: 123, preview_max_chars: 456}\ntoolsets: [core_file]\n',
    )
    write(
        root / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test: {model: openai/test, api_key: hidden-key}\n  summary: {model: openai/test}\n",
    )
    return load_project_config(root)


def test_effective_values_sources_and_secret_projection_are_independent(tmp_path):
    from src.lib.config.config import build_effective_agent_config_snapshot
    from src.tui_bridge.application_studio import application_detail

    base = project_config(tmp_path)
    app = tmp_path / "applications/group/demo"
    write(
        app / "config/system.yaml", "todo: {mode: on}\ncontext_engine: {min_chars: 789}\ntoolsets: [markdown_report]\n"
    )
    path = write(
        app / "workflows/root.yaml",
        BASE
        + """todo: {mode: off}
smart_summary: "false"
toolsets: []
model_request_headers:
  headers: {Authorization: private-header-token}
""",
    )
    parsed = load_agent_definition(path)
    snapshot = build_effective_agent_config_snapshot(parsed, base_config=base)
    detail = application_detail(
        tmp_path, "group/demo", systems=[{"path": str(path.relative_to(tmp_path)), "application_id": "group/demo"}]
    )
    public = detail["agents"][0]["effective_config"]
    assert snapshot.values["todo"] == {"mode": "off"}
    assert snapshot.values["smart_summary"] is False
    assert snapshot.values["toolsets"] == []
    assert snapshot.values["context_engine"] == {"min_chars": 789, "preview_max_chars": 456}
    assert public["values"]["todo"] == {"mode": "off"}
    assert public["values"]["context_engine"] == {"min_chars": 789, "preview_max_chars": 456}
    assert public["sources"]["todo.mode"]["source"] == "agent"
    assert public["sources"]["context_engine.min_chars"]["source"] == "application"
    assert public["sources"]["context_engine.preview_max_chars"]["source"] == "global"
    assert "private-header-token" not in str(detail) and "hidden-key" not in str(detail)
    assert snapshot.values["model_request_headers"]["headers"]["Authorization"] == "private-header-token"
    public["values"]["context_engine"]["min_chars"] = -1
    snapshot.values["context_engine"]["min_chars"] = -2
    assert base.raw["context_engine"]["min_chars"] == 123
    assert parsed["todo"]["mode"] is False  # Original YAML source retained.


@pytest.mark.parametrize(
    "field,value",
    [
        ("todo", "[]"),
        ("context_engine", "wrong"),
        ("skills", "[]"),
        ("hooks", "null"),
        ("smart_summary", "wrong"),
        ("tool_access_control", "false"),
    ],
)
def test_invalid_overrides_are_never_dropped(tmp_path, field, value):
    from src.lib.config.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE + f"{field}: {value}\n")
    parsed = load_agent_definition(path)
    with pytest.raises(ValueError, match=field):
        build_effective_agent_config_snapshot(parsed, base_config=base)
    errors = validate_agent_definition(tmp_path, str(path), parsed)
    assert any(field in error for error in errors)


def test_model_catalog_selection_preserves_case_and_empty_fallback(tmp_path):
    from src.application.definition import selected_model_type

    project_config(tmp_path)
    catalog = ("TEST", {"test": {"model": "openai/test"}, "summary": {"model": "openai/test"}})
    assert selected_model_type({"model_type": "  TEST "}, catalog) == "test"
    assert selected_model_type({"model_type": ""}, catalog) == "test"
    assert selected_model_type({"model_type": "  "}, catalog) == "test"
    with pytest.raises(ValueError, match="not defined"):
        selected_model_type({"model_type": "missing"}, catalog)


def test_summary_profile_requirement_matches_runtime_catalog(tmp_path):
    from src.lib.config.config import load_project_config

    write(tmp_path / "config/llm.yaml", "model:\n  default_model_type: test\n  test: {model: openai/test}\n")
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE)
    with pytest.raises(ValueError, match="summary.*required"):
        load_project_config(tmp_path)
    errors = validate_agent_definition(tmp_path, str(path), load_agent_definition(path))
    assert any("summary" in error and "required" in error for error in errors)


def test_running_graph_and_config_remain_pinned_while_next_call_observes_edits(tmp_path):
    from src.application.definition import prepare_application_definition

    base = project_config(tmp_path)
    app = tmp_path / "applications/nested/demo"
    path = write(app / "workflows/root.yaml", BASE + "worker_agents: [{path: child.md}]\n")
    worker = write(path.parent / "worker_agents/child.md", "```yaml\n" + BASE + SCHEMA + "```\nOriginal task.\n")
    app_config = write(app / "config/system.yaml", "todo: {mode: on}\n")
    first = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)
    worker.write_text("```yaml\n" + BASE + SCHEMA + "```\nEdited task.\n")
    app_config.write_text("todo: {mode: off}\n")
    second = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)
    first_worker = first["_worker_definitions"][str(worker)]
    second_worker = second["_worker_definitions"][str(worker)]
    assert first_worker["workflow"] == "Original task."
    assert second_worker["workflow"] == "Edited task."
    assert first_worker["_effective_agent_config_snapshot"].values["todo"]["mode"] == "on"
    assert second_worker["_effective_agent_config_snapshot"].values["todo"]["mode"] == "off"


def test_invalid_worker_is_rejected_before_any_run_allocation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import src.runner as runner

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE + "worker_agents: [{path: child.yaml}]\n")
    write(path.parent / "worker_agents/child.yaml", BASE)
    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(runner, "generate_runtime_id", lambda *args: pytest.fail("allocated a Run for invalid Worker"))
    events = []
    with pytest.raises(ValueError, match="agent_function_schema is required"):
        runner.execute_app(path, event_sink=events.append)
    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"


def test_file_tool_cache_uses_content_and_preserves_existing_callable(tmp_path):
    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

    class DefinitionTool:
        def __init__(self, config, **kwargs):
            self.workflow = config["workflow"]

        def agent_as_tool(self):
            def work():
                return self.workflow

            return work

    path = write(tmp_path / "worker.yaml", BASE)
    YamlAgentFactory.clear_tool_cache()
    first = YamlAgentFactory.create_agent_as_tool(path, agent_class=DefinitionTool)
    write(path, BASE.replace("Run the task.", "Use new definition."))
    second = YamlAgentFactory.create_agent_as_tool(path, agent_class=DefinitionTool)
    assert first() == "Run the task."
    assert second() == "Use new definition."
    assert first is not second
    YamlAgentFactory.clear_tool_cache()


def test_worker_resolution_rejects_symlink_escape_and_allows_absolute_file(tmp_path):
    from src.application.definition import resolve_worker_path

    source = write(tmp_path / "project/applications/demo/workflows/root.yaml", BASE)
    external = write(tmp_path / "outside/worker.yaml", BASE + SCHEMA)
    link = source.parent / "worker_agents"
    link.symlink_to(external.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        resolve_worker_path(tmp_path / "project", source, "worker.yaml")
    assert resolve_worker_path(tmp_path / "project", source, str(external)) == external


def test_worker_agents_prefix_is_relative_to_supervisor_source(tmp_path):
    from src.application.definition import resolve_worker_path

    source = tmp_path / "applications/nested/demo/workflows/root.yaml"
    assert resolve_worker_path(tmp_path, source, "worker_agents/worker.md") == source.parent / "worker_agents/worker.md"


def test_readonly_inspection_with_hooks_mcp_and_tools_has_no_runtime_side_effects(tmp_path):
    import json
    import subprocess
    import sys

    project_config(tmp_path)
    write(
        tmp_path / "applications/demo/workflows/root.yaml",
        BASE
        + """tools: [{name: read_file}]
hooks:
  SessionStart:
    - id: forbidden-side-effect
      command: touch must-not-exist
mcp_servers: config/test.mcp.json
""",
    )
    write(tmp_path / "config/test.mcp.json", '{"mcpServers":{"test":{"type":"http","url":"http://127.0.0.1:9/mcp"}}}')
    program = """
import json, sys
from pathlib import Path
from src.tui_bridge.bridge import TuiBridge
root = Path(sys.argv[1])
detail = TuiBridge(root).dispatch('application.detail', {'application_id':'demo'})
assert detail['agents'][0]['validation']['valid'], detail
for prefix in ('litellm', 'src.lib.smolagents.agent.base_agent', 'src.tools.file_ops', 'src.tools.shell', 'src.tools.search'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
assert not (root / '.agentloom').exists()
assert not (root / 'must-not-exist').exists()
print(json.dumps({'valid': True}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"valid": True}


@pytest.mark.parametrize("value", ["42", "[false]", "{timeout: 2}", "{paths: wrong}", "{path: config/missing.json}"])
def test_mcp_static_errors_reject_before_runtime(tmp_path, value):
    project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE + f"mcp_servers: {value}\n")
    errors = validate_agent_definition(tmp_path, str(path), load_agent_definition(path))
    assert any("mcp" in error.lower() for error in errors), errors


def test_mcp_snapshot_preserves_credentials_and_normalizes_options(tmp_path):
    from src.application.definition import prepare_application_definition

    base = project_config(tmp_path)
    path = write(
        tmp_path / "applications/demo/workflows/root.yaml",
        BASE + 'mcp_servers: {path: config/mcp.json, timeout: "12", tool_name_prefix: "false"}\n',
    )
    write(
        tmp_path / "config/mcp.json",
        '{"mcpServers":{"test":{"type":"http","url":"https://localhost/mcp","headers":{"Authorization":"token"}}}}',
    )
    prepared = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)
    settings = prepared["_effective_agent_config_snapshot"].values["_mcp_settings_snapshot"]
    assert settings.timeout == 12 and settings.tool_name_prefix is False
    assert settings.configs[0].headers == {"Authorization": "token"}


def test_hook_projection_uses_complete_id_replacement_and_disabling(tmp_path):
    from src.application.presentation import configuration_projection
    from src.lib.config.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    base.raw["hooks"] = {
        "PreToolUse": [
            {"id": "keep", "command": "old", "timeout": 7},
            {"id": "remove", "command": "old"},
        ]
    }
    path = write(
        tmp_path / "applications/demo/workflows/root.yaml",
        BASE
        + """hooks:
  PreToolUse:
    - {id: keep, command: new}
    - {id: remove, enabled: false}
""",
    )
    snapshot = build_effective_agent_config_snapshot(load_agent_definition(path), base_config=base)
    projection = configuration_projection(snapshot, tmp_path)
    assert projection["values"]["hooks"] == {"PreToolUse": [{"id": "keep", "matcher": "*", "timeout": 20.0}]}
    assert projection["hook_plan"] == [
        {
            "id": "keep",
            "matcher": "*",
            "timeout": 20.0,
            "event": "PreToolUse",
            "source": "agent",
            "source_path": "applications/demo/workflows/root.yaml",
        }
    ]


def test_mcp_connection_failure_is_reported_in_execution_stage(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    import src.mcp.manager as manager_module
    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
    from src.mcp.config import McpServerConfig, McpSettings

    manager = MagicMock()
    manager.get_server_status.return_value = {
        "configured-server": {"connected": False, "error": "secret in provider error"}
    }
    monkeypatch.setattr(manager_module, "McpManager", lambda settings: manager)
    settings = McpSettings(configs=[McpServerConfig(name="configured-server", type="stdio", command="server")])
    with pytest.raises(RuntimeError, match="MCP connection failed for server") as captured:
        YamlAgentFactory.get_tools_from_config(
            {"tools": []},
            effective_agent_config={
                "default_toolsets": [],
                "mcp_servers": "config/mcp.json",
                "_mcp_settings_snapshot": settings,
            },
        )
    assert "secret" not in str(captured.value)
    manager.disconnect_all.assert_called_once()


def test_execute_app_refreshes_global_config_between_calls_but_pins_running_read(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import src.lib.config.config as config_module
    import src.runner as runner

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE)
    monkeypatch.setattr(config_module, "_ACTIVE_CONFIG", base)
    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    seen = []

    class StopAfterPreflight(Exception):
        pass

    def inspect_allocation(kind):
        current = config_module.get_config()
        seen.append(config_module.build_effective_agent_config(load_agent_definition(path))["todo"]["mode"])
        write(tmp_path / "config/system.yaml", "todo: {mode: on}\ntoolsets: []\n")
        assert config_module.get_config() is current
        assert config_module.build_effective_agent_config(load_agent_definition(path))["todo"]["mode"] == seen[-1]
        raise StopAfterPreflight

    monkeypatch.setattr(runner, "generate_runtime_id", inspect_allocation)
    for _ in range(2):
        with pytest.raises(StopAfterPreflight):
            runner.execute_app(path)
    assert seen == ["auto", "on"]
    assert config_module.get_config() is base
    assert base.raw["todo"]["mode"] == "auto"


def test_programmatic_config_override_remains_authoritative(tmp_path):
    from src.lib.config.config import fresh_invocation_config

    base = project_config(tmp_path)
    base.raw["todo"]["mode"] = "off"
    write(tmp_path / "config/system.yaml", "todo: {mode: on}\n")
    snapshot = fresh_invocation_config(base)
    assert snapshot.raw["todo"]["mode"] == "off"
    assert snapshot is not base


def test_model_cache_tracks_profile_content_across_invocations(tmp_path):
    from src.lib.config.config import bind_config, fresh_invocation_config
    from src.lib.smolagents.models.model_manager import ModelManager
    from src.lib.smolagents.models.model_types import ModelType

    base = project_config(tmp_path)
    with bind_config(fresh_invocation_config(base)):
        manager = ModelManager()
        first = manager.get_litellm_config(ModelType("test"))
        write(
            tmp_path / "config/llm.yaml",
            "model:\n  default_model_type: test\n  test: {model: openai/changed, api_key: updated-key}\n  summary: {model: openai/test}\n",
        )
        still_running = manager.get_litellm_config(ModelType("test"))
        assert still_running is first
    with bind_config(fresh_invocation_config(base)):
        second = manager.get_litellm_config(ModelType("test"))
    assert first["model"] == "openai/test"
    assert second["model"] == "openai/changed"
    assert second["api_key"] == "updated-key"
