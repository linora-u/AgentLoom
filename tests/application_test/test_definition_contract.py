from pathlib import Path

import pytest
from agentloom.application.definition import load_agent_definition, validate_agent_definition
from agentloom.application.validation import AgentConfigNormalizer


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


BASE = "name: demo\nagent_runtime: smolagents\ndescription: Demo\nworkflow: Run the task.\n"
SCHEMA = """input_schema:
  type: object
  properties:
    task:
      type: string
      description: The task.
  required: [task]
  additionalProperties: false
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
        catalog=("test", {"test": {"model": "openai/test", "adapter": "openai_chat"}}),
    )
    assert any("cycle" in error.lower() and "root.yaml" in error for error in errors), errors


def project_config(root):
    from agentloom.configuration.config import load_project_config

    write(
        root / "config/system.yaml",
        'runtime_options: {todo_mode: "auto", smart_summary: true}\ncontext_engine: {min_chars: 123, preview_max_chars: 456}\ntoolsets: [core_file]\n',
    )
    write(
        root / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test: {model: openai/test, adapter: openai_chat, api_key: hidden-key}\n  summary: {model: openai/test, adapter: openai_chat}\n",
    )
    return load_project_config(root)


def test_effective_values_sources_and_secret_projection_are_independent(tmp_path):
    from agentloom.application.studio.application_studio import application_detail
    from agentloom.configuration.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    app = tmp_path / "applications/group/demo"
    write(
        app / "config/system.yaml",
        'runtime_options: {todo_mode: "on"}\ncontext_engine: {min_chars: 789}\ntoolsets: [markdown_report]\n',
    )
    path = write(
        app / "workflows/root.yaml",
        BASE
        + """runtime_options:
  todo_mode: "off"
  smart_summary: false
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
    assert snapshot.values["runtime_options"] == {"todo_mode": "off", "smart_summary": False}
    assert snapshot.values["toolsets"] == []
    assert snapshot.values["context_engine"] == {"min_chars": 789, "preview_max_chars": 456}
    assert public["values"]["runtime_options"] == {"todo_mode": "off", "smart_summary": False}
    assert public["values"]["context_engine"] == {"min_chars": 789, "preview_max_chars": 456}
    assert public["sources"]["runtime_options.todo_mode"]["source"] == "agent"
    assert public["sources"]["context_engine.min_chars"]["source"] == "application"
    assert public["sources"]["context_engine.preview_max_chars"]["source"] == "global"
    assert "private-header-token" not in str(detail) and "hidden-key" not in str(detail)
    assert snapshot.values["model_request_headers"]["headers"]["Authorization"] == "private-header-token"
    public["values"]["context_engine"]["min_chars"] = -1
    snapshot.values["context_engine"]["min_chars"] = -2
    assert base.raw["context_engine"]["min_chars"] == 123
    assert parsed["runtime_options"] == {"todo_mode": "off", "smart_summary": False}


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_engine", "wrong"),
        ("skills", "[]"),
        ("hooks", "null"),
        ("tool_access_control", "false"),
    ],
)
def test_invalid_overrides_are_never_dropped(tmp_path, field, value):
    from agentloom.configuration.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE + f"{field}: {value}\n")
    parsed = load_agent_definition(path)
    with pytest.raises(ValueError, match=field):
        build_effective_agent_config_snapshot(parsed, base_config=base)
    errors = validate_agent_definition(tmp_path, str(path), parsed)
    assert any(field in error for error in errors)


@pytest.mark.parametrize(
    "value",
    ["[]", "wrong", "{todo_mode: unsupported}", "{todo_mode: off}", "{smart_summary: wrong}"],
)
def test_invalid_runtime_options_are_preserved_for_backend_validation(tmp_path, value):
    from agentloom.configuration.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE + f"runtime_options: {value}\n")
    parsed = load_agent_definition(path)
    snapshot = build_effective_agent_config_snapshot(parsed, base_config=base)
    invalid = parsed["runtime_options"]
    if isinstance(invalid, dict):
        for key, setting in invalid.items():
            assert snapshot.values["runtime_options"][key] == setting
    else:
        assert snapshot.values["runtime_options"] == invalid
    errors = validate_agent_definition(tmp_path, str(path), parsed)
    assert any("runtime_options" in error for error in errors), errors


@pytest.mark.parametrize("field,value", [("todo", "[]"), ("smart_summary", "wrong")])
def test_historical_smol_fields_are_ignored_without_conversion_or_rejection(tmp_path, field, value):
    from agentloom.application.runtime_options import normalize_runtime_options
    from agentloom.configuration.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    path = write(
        tmp_path / "applications/demo/workflows/root.yaml",
        BASE + f'{field}: {value}\nruntime_options: {{todo_mode: "off", smart_summary: false}}\n',
    )
    parsed = load_agent_definition(path)
    snapshot = build_effective_agent_config_snapshot(parsed, base_config=base)
    assert field not in snapshot.values
    options, sources = normalize_runtime_options(parsed, snapshot=snapshot, agent_root=tmp_path)
    assert options["todo_mode"] == "off"
    assert options["smart_summary"] is False
    assert sources["todo_mode"].endswith(":runtime_options.todo_mode")
    assert sources["smart_summary"].endswith(":runtime_options.smart_summary")
    assert validate_agent_definition(tmp_path, str(path), parsed) == []


def test_model_catalog_selection_preserves_case_and_empty_fallback(tmp_path):
    from agentloom.application.definition import selected_model_type

    project_config(tmp_path)
    catalog = (
        "TEST",
        {
            "test": {"model": "openai/test", "adapter": "openai_chat"},
            "summary": {"model": "openai/test", "adapter": "openai_chat"},
        },
    )
    assert selected_model_type({"model_type": "  TEST "}, catalog) == "test"
    assert selected_model_type({"model_type": ""}, catalog) == "test"
    assert selected_model_type({"model_type": "  "}, catalog) == "test"
    with pytest.raises(ValueError, match="not defined"):
        selected_model_type({"model_type": "missing"}, catalog)


def test_summary_profile_requirement_matches_runtime_catalog(tmp_path):
    from agentloom.configuration.config import load_project_config

    write(
        tmp_path / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test: {model: openai/test, adapter: openai_chat}\n",
    )
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE)
    with pytest.raises(ValueError, match="summary.*required"):
        load_project_config(tmp_path)
    errors = validate_agent_definition(tmp_path, str(path), load_agent_definition(path))
    assert any("summary" in error and "required" in error for error in errors)


def test_running_graph_and_config_remain_pinned_while_next_call_observes_edits(tmp_path):
    from agentloom.application.definition import prepare_application_definition

    base = project_config(tmp_path)
    app = tmp_path / "applications/nested/demo"
    path = write(app / "workflows/root.yaml", BASE + "worker_agents: [{path: child.md}]\n")
    worker = write(path.parent / "worker_agents/child.md", "```yaml\n" + BASE + SCHEMA + "```\nOriginal task.\n")
    app_config = write(app / "config/system.yaml", 'runtime_options: {todo_mode: "on"}\n')
    first = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)
    worker.write_text("```yaml\n" + BASE + SCHEMA + "```\nEdited task.\n")
    app_config.write_text('runtime_options: {todo_mode: "off"}\n')
    second = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)
    first_worker = first["_worker_definitions"][str(worker)]
    second_worker = second["_worker_definitions"][str(worker)]
    assert first_worker["workflow"] == "Original task."
    assert second_worker["workflow"] == "Edited task."
    assert first_worker["_effective_agent_config_snapshot"].values["runtime_options"]["todo_mode"] == "on"
    assert second_worker["_effective_agent_config_snapshot"].values["runtime_options"]["todo_mode"] == "off"


def test_invalid_worker_is_rejected_before_any_run_allocation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import agentloom.application.runner as runner

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE + "worker_agents: [{path: child.yaml}]\n")
    write(
        path.parent / "worker_agents/child.yaml",
        BASE + "input_schema: {type: array, items: {type: string}}\n",
    )
    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(runner, "generate_runtime_id", lambda *args: pytest.fail("allocated a Run for invalid Worker"))
    events = []
    with pytest.raises(ValueError, match="input_schema root type must be object"):
        runner.execute_app(path, event_sink=events.append)
    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"


@pytest.mark.parametrize("target", ["supervisor", "worker"])
@pytest.mark.parametrize(
    "manifests,message",
    [
        (
            {"agent-skills/bad_name": "---\nname: bad_name\ndescription: Invalid name.\n---\nInstructions.\n"},
            "kebab-case",
        ),
        ({"agent-skills/broken": "---\nname: [unterminated\n---\nInstructions.\n"}, "Invalid skill frontmatter"),
        ({"agent-skills/missing": "---\nname: missing\n---\nInstructions.\n"}, "field 'description'"),
        (
            {
                "agent-skills/same-name": "---\nname: same-name\ndescription: First.\n---\nFirst.\n",
                "other-skills/same-name": "---\nname: same-name\ndescription: Second.\n---\nSecond.\n",
            },
            "Duplicate skill name 'same-name'",
        ),
    ],
    ids=["invalid-name", "malformed-frontmatter", "missing-description", "duplicate-name"],
)
def test_invalid_discovered_skill_is_rejected_before_any_run_allocation(
    tmp_path, monkeypatch, target, manifests, message
):
    from types import SimpleNamespace

    import agentloom.application.runner as runner
    from agentloom.application.studio.application_studio import application_detail

    base = project_config(tmp_path)
    app = tmp_path / "applications/demo"
    skill_config = "skills: {paths: [agent-skills, other-skills]}\n"
    path = write(
        app / "workflows/root.yaml",
        BASE + "worker_agents: [{path: child.yaml}]\n" + (skill_config if target == "supervisor" else ""),
    )
    write(path.parent / "worker_agents/child.yaml", BASE + SCHEMA + (skill_config if target == "worker" else ""))
    for directory, content in manifests.items():
        write(app / directory / "SKILL.md", content)

    detail = application_detail(
        tmp_path, "demo", systems=[{"path": str(path.relative_to(tmp_path)), "application_id": "demo"}]
    )
    assert detail["application"]["health"] == "invalid"
    agent = detail["agents"][0]
    assert any(message in error for error in agent["validation"]["errors"])
    if target == "worker":
        assert any(message in error for error in agent["workers"][0]["validation"]["errors"])
    errors = validate_agent_definition(tmp_path, str(path), load_agent_definition(path))
    assert any(message in error for error in errors), errors

    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(runner, "generate_runtime_id", lambda *args: pytest.fail("allocated a Run for invalid Skill"))
    events = []
    with pytest.raises(ValueError, match=message):
        runner.execute_app(path, event_sink=events.append)
    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"


def test_skill_instructions_are_pinned_for_each_runtime_definition_and_refresh_on_next_preparation(
    tmp_path, monkeypatch
):
    import json
    import logging

    from agentloom.application.agent import AgentRoleProfile, AgentType, RoleDrivenAgent
    from agentloom.application.definition import prepare_application_definition
    from agentloom.application.presentation import configuration_projection
    from agentloom.execution.skills.catalog import SkillCatalog

    class SnapshotAgent(RoleDrivenAgent):
        def _role_profile(self):
            return AgentRoleProfile(agent_type=AgentType.WORKER)

        def _get_tools(self):
            return []

    def skill(path, instructions):
        return write(path, f"---\nname: shared-review\ndescription: Shared review.\n---\n{instructions}\n")

    base = project_config(tmp_path)
    app = tmp_path / "applications/demo"
    path = write(app / "workflows/root.yaml", BASE + "worker_agents: [{path: child.yaml}]\n")
    worker = write(path.parent / "worker_agents/child.yaml", BASE + SCHEMA + "skills: {paths: [agent-skills]}\n")
    skill(tmp_path / "skills/shared-review/SKILL.md", "Project instructions.")
    app_skill = skill(app / "skills/shared-review/SKILL.md", "Original application instructions.")
    agent_skill = skill(app / "agent-skills/shared-review/SKILL.md", "Original agent instructions.")
    first = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)
    skill(app_skill, "Edited application instructions.")
    skill(agent_skill, "Edited agent instructions.")
    second = prepare_application_definition(tmp_path, path, load_agent_definition(path), base_config=base)

    monkeypatch.setattr(SkillCatalog, "discover", lambda *args, **kwargs: pytest.fail("runtime reread pinned Skills"))
    for prepared, prefix in [(first, "Original"), (second, "Edited")]:
        definitions = [(prepared, "application"), (prepared["_worker_definitions"][str(worker)], "agent")]
        for definition, scope in definitions:
            agent = SnapshotAgent(config=definition, model=object(), logger=logging.getLogger(__name__))
            catalog = agent._skill_catalog
            assert catalog.summaries()[0].scope == scope
            assert catalog.activate("shared-review").instructions == f"{prefix} {scope} instructions.\n"
            public = json.dumps(configuration_projection(definition["_effective_agent_config_snapshot"], tmp_path))
            assert "_skill_catalog_snapshot" not in public
            assert "instructions." not in public
    assert "_skill_catalog_snapshot" not in base.raw


def test_studio_uses_the_catalog_parsed_during_its_single_definition_inspection(tmp_path, monkeypatch):
    import json

    from agentloom.application.studio.application_studio import application_detail
    from agentloom.execution.skills.catalog import SkillCatalog

    project_config(tmp_path)
    app = tmp_path / "applications/demo"
    path = write(app / "workflows/root.yaml", BASE)
    manifest = write(
        app / "skills/review/SKILL.md",
        "---\nname: review\ndescription: Original summary.\n---\nPrivate instructions.\n",
    )
    discover = SkillCatalog.discover
    calls = []

    def edit_after_discovery(sources, **kwargs):
        catalog = discover(sources, **kwargs)
        calls.append(catalog)
        write(manifest, "---\nname: review\ndescription: Edited summary.\n---\nEdited private instructions.\n")
        return catalog

    monkeypatch.setattr(SkillCatalog, "discover", edit_after_discovery)
    systems = [{"path": str(path.relative_to(tmp_path)), "application_id": "demo"}]
    first = application_detail(tmp_path, "demo", systems=systems)
    assert len(calls) == 1
    assert first["agents"][0]["skills"][0]["description"] == "Original summary."
    assert "Private instructions." not in json.dumps(first)
    second = application_detail(tmp_path, "demo", systems=systems)
    assert len(calls) == 2
    assert second["agents"][0]["skills"][0]["description"] == "Edited summary."


def test_fresh_file_tool_definition_preserves_existing_callable(tmp_path):
    from agentloom.application.factory import YamlAgentFactory

    class DefinitionTool:
        def __init__(self, config, **kwargs):
            self.workflow = config["workflow"]

        def agent_as_tool(self):
            def work():
                return self.workflow

            return work

    path = write(tmp_path / "worker.yaml", BASE)
    first = YamlAgentFactory.create_agent_as_tool(path, agent_class=DefinitionTool)
    write(path, BASE.replace("Run the task.", "Use new definition."))
    second = YamlAgentFactory.create_agent_as_tool(path, agent_class=DefinitionTool)
    assert first() == "Run the task."
    assert second() == "Use new definition."
    assert first is not second


def test_worker_resolution_rejects_symlink_escape_and_allows_absolute_file(tmp_path):
    from agentloom.application.definition import resolve_worker_path

    source = write(tmp_path / "project/applications/demo/workflows/root.yaml", BASE)
    external = write(tmp_path / "outside/worker.yaml", BASE + SCHEMA)
    link = source.parent / "worker_agents"
    link.symlink_to(external.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        resolve_worker_path(tmp_path / "project", source, "worker.yaml")
    assert resolve_worker_path(tmp_path / "project", source, str(external)) == external


def test_worker_agents_prefix_is_relative_to_supervisor_source(tmp_path):
    from agentloom.application.definition import resolve_worker_path

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
    write(
        tmp_path / "applications/demo/skills/review/SKILL.md",
        "---\nname: review\ndescription: Review.\n---\nPrivate review instructions.\n",
    )
    program = """
import json, sys
from pathlib import Path
from agentloom.application.studio.query_service import StudioQueryService
root = Path(sys.argv[1])
detail = StudioQueryService(root).application_detail('demo')
assert detail['agents'][0]['validation']['valid'], detail
assert detail['agents'][0]['skills'][0]['name'] == 'review'
assert 'Private review instructions.' not in json.dumps(detail)
from agentloom.application.definition import load_agent_definition, prepare_application_definition
from agentloom.configuration.config import load_project_config
path = root / 'applications/demo/workflows/root.yaml'
prepared = prepare_application_definition(root, path, load_agent_definition(path), base_config=load_project_config(root))
assert prepared['_skill_catalog_snapshot'].activate('review').instructions == 'Private review instructions.\\n'
for prefix in ('litellm', 'agentloom.application.agent', 'agentloom.runtimes.smolagents.tools.file_ops', 'agentloom.runtimes.smolagents.tools.shell', 'agentloom.runtimes.smolagents.tools.search'):
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
    from agentloom.application.definition import prepare_application_definition

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
    from agentloom.application.presentation import configuration_projection
    from agentloom.configuration.config import build_effective_agent_config_snapshot

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

    import agentloom.integrations.mcp.manager as manager_module
    from agentloom.application.factory import YamlAgentFactory
    from agentloom.integrations.mcp.config import McpServerConfig, McpSettings

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

    import agentloom.application.runner as runner
    import agentloom.configuration.config as config_module

    base = project_config(tmp_path)
    path = write(tmp_path / "applications/demo/workflows/root.yaml", BASE)
    monkeypatch.setattr(config_module, "_ACTIVE_CONFIG", base)
    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    seen = []

    class StopAfterPreflight(Exception):
        pass

    def inspect_allocation(kind):
        current = config_module.get_config()
        seen.append(
            config_module.build_effective_agent_config(load_agent_definition(path))["runtime_options"]["todo_mode"]
        )
        write(tmp_path / "config/system.yaml", 'runtime_options: {todo_mode: "on"}\ntoolsets: []\n')
        assert config_module.get_config() is current
        assert (
            config_module.build_effective_agent_config(load_agent_definition(path))["runtime_options"]["todo_mode"]
            == seen[-1]
        )
        raise StopAfterPreflight

    monkeypatch.setattr(runner, "generate_runtime_id", inspect_allocation)
    for _ in range(2):
        with pytest.raises(StopAfterPreflight):
            runner.execute_app(path)
    assert seen == ["auto", "on"]
    assert config_module.get_config() is base
    assert base.raw["runtime_options"]["todo_mode"] == "auto"


def test_programmatic_config_override_remains_authoritative(tmp_path):
    from agentloom.configuration.config import fresh_invocation_config

    base = project_config(tmp_path)
    base.raw["runtime_options"]["todo_mode"] = "off"
    write(tmp_path / "config/system.yaml", 'runtime_options: {todo_mode: "on"}\n')
    snapshot = fresh_invocation_config(base)
    assert snapshot.raw["runtime_options"]["todo_mode"] == "off"
    assert snapshot is not base


def test_model_cache_tracks_profile_content_across_invocations(tmp_path):
    from agentloom.configuration.config import bind_config, fresh_invocation_config
    from agentloom.runtimes.smolagents.models.model_manager import ModelManager
    from agentloom.runtimes.smolagents.models.model_types import ModelType

    base = project_config(tmp_path)
    with bind_config(fresh_invocation_config(base)):
        manager = ModelManager()
        first = manager.get_litellm_config(ModelType("test"))
        write(
            tmp_path / "config/llm.yaml",
            "model:\n  default_model_type: test\n  test: {model: openai/changed, adapter: openai_chat, api_key: updated-key}\n  summary: {model: openai/test, adapter: openai_chat}\n",
        )
        still_running = manager.get_litellm_config(ModelType("test"))
        assert still_running is first
    with bind_config(fresh_invocation_config(base)):
        second = manager.get_litellm_config(ModelType("test"))
    assert first["model"] == "openai/test"
    assert second["model"] == "openai/changed"
    assert second["api_key"] == "updated-key"


def test_public_connection_urls_never_expose_authentication(tmp_path):
    import json

    from agentloom.application.studio.application_studio import application_detail
    from agentloom.configuration.config import build_effective_agent_config_snapshot

    base = project_config(tmp_path)
    url = "https://synthetic-user:synthetic-password@example.invalid/mcp?access_token=synthetic-token"
    path = write(
        tmp_path / "applications/demo/workflows/root.yaml",
        BASE + f"mcp_servers:\n  remote:\n    url: {url}\n    headers:\n      Authorization: synthetic-header\n",
    )
    snapshot = build_effective_agent_config_snapshot(load_agent_definition(path), base_config=base)
    detail = application_detail(
        tmp_path,
        "demo",
        systems=[{"path": str(path.relative_to(tmp_path)), "application_id": "demo"}],
    )
    public = detail["agents"][0]["effective_config"]
    for secret in ("synthetic-user", "synthetic-password", "synthetic-token", "synthetic-header"):
        assert secret not in json.dumps(public)
    assert public["values"]["mcp_servers"]["remote"]["url"] == "[redacted]"
    assert snapshot.values["mcp_servers"]["remote"]["url"] == url
    assert snapshot.values["mcp_servers"]["remote"]["headers"]["Authorization"] == "synthetic-header"


@pytest.mark.parametrize("target", ["supervisor", "worker"])
@pytest.mark.parametrize("suffix", [".yaml", ".md"])
def test_removed_fields_reject_consistently_before_run_allocation(tmp_path, monkeypatch, target, suffix):
    from types import SimpleNamespace

    import agentloom.application.runner as runner
    from agentloom.application.definition import prepare_application_definition
    from agentloom.application.factory import YamlConfiguredAgent, YamlConfiguredSupervisorAgent
    from agentloom.application.readiness import validate_runtime_agent_config, validate_runtime_worker_config
    from agentloom.application.studio.domain_actions import execute_domain_action
    from agentloom.application.studio.query_service import StudioQueryService

    base = project_config(tmp_path)
    path = tmp_path / f"applications/demo/workflows/root{suffix}"
    worker = path.parent / f"worker_agents/child{suffix}"
    removed = "tools_mapping: {Claude: {Read: read_file}}\n"
    supervisor_text = BASE + f"worker_agents: [{{path: child{suffix}}}]\n"
    worker_text = BASE + SCHEMA
    for role, source, body in (("supervisor", path, supervisor_text), ("worker", worker, worker_text)):
        if role == target:
            body += removed
        write(source, f"```yaml\n{body}```\nRun the task.\n" if source.suffix == ".md" else body)

    message = "Configuration error: tools_mapping was removed; Skills do not grant tools"
    invalid_path = path if target == "supervisor" else worker
    definition = load_agent_definition(path)
    invalid_definition = load_agent_definition(invalid_path)
    readiness = validate_runtime_agent_config if target == "supervisor" else validate_runtime_worker_config
    with pytest.raises(ValueError, match="tools_mapping was removed") as readiness_error:
        readiness(invalid_definition, invalid_path, agent_root=tmp_path)
    assert str(readiness_error.value) == message
    errors = validate_agent_definition(tmp_path, str(path), definition)
    assert f"{invalid_path}: {message}" in errors
    with pytest.raises(ValueError, match="tools_mapping was removed"):
        prepare_application_definition(tmp_path, path, definition, base_config=base)

    detail = StudioQueryService(tmp_path).application_detail("demo")
    assert detail["application"]["health"] == "invalid"
    entry = detail["agents"][0]
    if target == "worker":
        entry = entry["workers"][0]
    assert f"{invalid_path}: {message}" in entry["validation"]["errors"]
    public = execute_domain_action(
        tmp_path,
        "application.validate",
        {"application_id": "demo"},
    )
    assert public["valid"] is False
    assert f"{invalid_path}: {message}" in public["errors"]

    # Exercise the existing runtime validation entry, without constructing a model.
    cls = YamlConfiguredSupervisorAgent if target == "supervisor" else YamlConfiguredAgent
    agent = object.__new__(cls)
    agent._config = invalid_definition
    agent._normalized = None
    agent._execution_normalized = None
    with pytest.raises(ValueError, match="tools_mapping was removed") as runtime_error:
        agent._validate_config()
    assert str(runtime_error.value) == message

    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(runner, "generate_runtime_id", lambda *args: pytest.fail("allocated a Run for a removed field"))
    events = []
    with pytest.raises(ValueError, match="tools_mapping was removed"):
        runner.execute_app(path, event_sink=events.append)
    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"


def test_removed_fields_in_markdown_supervisor_reject_before_run(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import agentloom.application.runner as runner
    from agentloom.application.definition import prepare_application_definition

    base = project_config(tmp_path)
    path = write(
        tmp_path / "applications/demo/workflows/root.md",
        "```yaml\n" + BASE + "tools_mapping: {}\n```\nRun the task.\n",
    )
    definition = load_agent_definition(path)
    errors = validate_agent_definition(tmp_path, str(path), definition)
    assert any("tools_mapping was removed" in error for error in errors)
    with pytest.raises(ValueError, match="tools_mapping was removed"):
        prepare_application_definition(tmp_path, path, definition, base_config=base)
    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(runner, "generate_runtime_id", lambda *args: pytest.fail("allocated a Run for a removed field"))
    events = []
    with pytest.raises(ValueError, match="tools_mapping was removed"):
        runner.execute_app(path, event_sink=events.append)
    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"


@pytest.mark.parametrize("replacement", ["symlink", "invalid"])
def test_scheduled_supervisor_target_is_revalidated_before_run_allocation(
    tmp_path,
    monkeypatch,
    replacement,
):
    from types import SimpleNamespace

    import agentloom.application.runner as runner

    base = project_config(tmp_path)
    path = write(
        tmp_path / "applications/demo/workflows/root.yaml",
        BASE,
    )
    if replacement == "symlink":
        outside = write(tmp_path.parent / f"{tmp_path.name}-outside.yaml", BASE)
        path.unlink()
        path.symlink_to(outside)
    else:
        path.write_text("name: invalid\n", encoding="utf-8")

    monkeypatch.setattr(runner, "C", SimpleNamespace(agent_root=tmp_path))
    monkeypatch.setattr(runner, "get_config", lambda: base)
    monkeypatch.setattr(
        runner,
        "generate_runtime_id",
        lambda *args: pytest.fail("allocated a Run for an invalid target"),
    )
    events = []

    with pytest.raises(ValueError):
        runner.execute_app(
            path.relative_to(tmp_path).as_posix(),
            event_sink=events.append,
            require_valid_supervisor_target=True,
        )

    assert not (tmp_path / ".agentloom").exists()
    assert len(events) == 1 and events[0].event == "run.rejected"
