"""Platform tools at the real YAML Application seam with a neutral runtime."""

import importlib.abc
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Barrier, Event

import psutil
import pytest
import yaml
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from agentloom.runtime.agent_runtime import AgentRuntimeResult, RuntimeCapabilities, RuntimeCheckpointEnvelope


@pytest.fixture
def platform_project(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.yaml").write_text(yaml.safe_dump({
        "checkpoint": {"enabled": False}, "self_learning": {"enabled": False},
        "default_toolsets": [], "smart_summary": False,
        "lsp_servers": {"enabled": False},
        "logging": {"console_enabled": False},
    }))
    (config / "llm.yaml").write_text(yaml.safe_dump({"model": {
        "default_model_type": "fixture", "fixture": {"model": "fixture/model", "adapter": "openai_chat"},
        "summary": {"model": "fixture/model", "adapter": "openai_chat"},
    }}))
    workflow = tmp_path / "applications" / "platform" / "workflows" / "root.yaml"
    workflow.parent.mkdir(parents=True)
    definition = {
        "name": "platform", "agent_runtime": "platform-fixture",
        "description": "Execute the selected platform tools.",
        "workflow": "Use the selected tools.", "tools": [], "toolsets": [],
    }
    programs = {}
    definitions = []

    class PlatformRuntime:
        runtime_id = "platform-fixture"
        capabilities = RuntimeCapabilities(True, True, True, True, goal=True, stop_hooks=True)

        def __init__(self, definition):
            self.definition = definition
            definitions.append(definition)

        def run(self, request):
            program = programs.get(self.definition.name, lambda _definition, _request: "done")
            return AgentRuntimeResult(state="success", output=program(self.definition, request))

        def snapshot(self):
            return RuntimeCheckpointEnvelope(
                runtime_id=self.runtime_id, runtime_version="fixture", state_schema_version=1, payload={},
            )

        def close(self):
            self.definition.tool_gateway.close()

    from agentloom.runtime.agent_runtime import build_builtin_runtime_registry
    registry = build_builtin_runtime_registry()
    registry.register("platform-fixture", capabilities=PlatformRuntime.capabilities, factory=PlatformRuntime)
    monkeypatch.setattr("agentloom.application.validation.build_builtin_runtime_registry", lambda: registry)
    monkeypatch.setattr("agentloom.runtime.agent.build_builtin_runtime_registry", lambda: registry)

    def run(**updates):
        workflow.write_text(yaml.safe_dump({**definition, **updates}))
        with bind_config(load_project_config(tmp_path)):
            return execute_app(workflow, file_logging=False)

    return tmp_path, workflow, programs, definitions, run


def test_application_without_optional_tools_does_not_load_lsp(platform_project, monkeypatch):
    _, _, _, definitions, run = platform_project
    attempted = []

    class NoLsp(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "agentloom.adapters.lsp" or fullname.startswith("agentloom.adapters.lsp."):
                attempted.append(fullname)
                raise ImportError("Unselected optional LSP dependency")

    for name in list(sys.modules):
        if name == "agentloom.adapters.lsp" or name.startswith("agentloom.adapters.lsp."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [NoLsp(), *sys.meta_path])
    assert run().output == "done"
    assert definitions[0].tool_gateway.definitions == ()
    assert attempted == [], "An unselected optional dependency was loaded by Application startup"


@pytest.fixture
def language_servers(monkeypatch):
    servers = []

    class LanguageServer:
        def __init__(self, language, project_root, max_restarts):
            self.language = language
            self.project_root = project_root
            self.is_healthy = False
            self.requests = []
            servers.append(self)

        def start(self):
            self.is_healthy = True

        def request(self, method, file, line, character):
            assert self.is_healthy
            self.requests.append((method, file, line, character))
            return {"contents": {"value": "answer: int = 42"}}

        def stop(self):
            self.is_healthy = False

    monkeypatch.setattr("agentloom.adapters.lsp.lsp_server_manager.LSPServerInstance", LanguageServer)
    return servers


def enable_lsp(root):
    config = root / "config" / "system.yaml"
    system = yaml.safe_load(config.read_text())
    system["lsp_servers"] = {"enabled": True, "servers": ["python"]}
    config.write_text(yaml.safe_dump(system))


def test_duplicate_lsp_configuration_rejected_before_starting_any_server(language_servers):
    from agentloom.adapters.lsp.config import LSPConfig
    from agentloom.adapters.lsp.lsp_server_manager import LSPServerManager

    manager = LSPServerManager()
    try:
        with pytest.raises(ValueError, match="Duplicate LSP server"):
            manager.initialize(LSPConfig.from_yaml({"servers": ["python", "python"]}), project_root=".")
        assert language_servers == []
    finally:
        manager.shutdown()


def test_selected_lsp_runs_lazily_and_releases_its_server(platform_project, language_servers):
    root, _, programs, _, run = platform_project
    source = root / "answer.py"
    source.write_text("answer = 42\n")
    enable_lsp(root)
    servers = language_servers

    def hover(definition, request):
        assert servers == [], "A selected tool should initialize its resource only on invocation"
        result = definition.tool_gateway.invoke(
            call_id="hover-1", tool_name="lsp_hover",
            arguments={"file_path": str(source), "line": 1, "character": 1},
        )
        assert result.status == "completed", result.model_content()
        return result.output

    programs["platform"] = hover
    result = run(tools=[{"name": "lsp_hover"}])
    assert "answer: int = 42" in result.output
    assert len(servers) == 1
    assert not servers[0].is_healthy
    assert servers[0].requests == [("request_hover", "answer.py", 0, 0)]


def test_closing_one_worker_keeps_the_other_workers_lsp_alive(platform_project, language_servers):
    root, workflow, programs, _, run = platform_project
    source = root / "answer.py"
    source.write_text("answer = 42\n")
    enable_lsp(root)
    worker = workflow.parent / "worker_agents" / "lsp_worker.yaml"
    worker.parent.mkdir()
    worker.write_text(yaml.safe_dump({
        "name": "lsp_worker", "agent_runtime": "platform-fixture",
        "description": "Inspect a symbol.", "workflow": "Inspect the requested symbol.",
        "tools": [{"name": "lsp_hover"}], "toolsets": [],
        "agent_function_schema": {
            "description": "Inspect a symbol.", "inputs": {"query": {"description": "Requested symbol."}},
            "output": {"description": "Symbol information."},
        },
    }))
    barrier = Barrier(2)
    release_second = Event()

    def worker_program(definition, request):
        from agentloom.runtime.trace import capture_explicit_execution_context
        assert capture_explicit_execution_context().agent_id == definition.instance_id
        def hover(call_id):
            result = definition.tool_gateway.invoke(call_id=call_id, tool_name="lsp_hover", arguments={
                "file_path": str(source), "line": 1, "character": 1,
            })
            assert result.status == "completed", result.model_content()
            assert "answer: int = 42" in result.output

        hover("first-hover")
        barrier.wait(timeout=10)
        if "second-worker" in request.task:
            assert release_second.wait(timeout=10)
            hover("after-sibling-close")
        return "symbol inspected"

    def supervisor_program(definition, request):
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(
                copy_context().run, definition.tool_gateway.invoke,
                call_id=f"worker-{index}", tool_name="lsp_worker", arguments={"query": query},
            ) for index, query in enumerate(("first-worker", "second-worker"))]
            try:
                assert calls[0].result(timeout=15).status == "completed"
                assert len(language_servers) == 2
                assert sum(server.is_healthy for server in language_servers) == 1
            finally:
                release_second.set()
            assert calls[1].result(timeout=15).status == "completed"
        return "workers closed independently"

    programs.update(platform=supervisor_program, lsp_worker=worker_program)
    assert run(worker_agents=[{"path": "lsp_worker.yaml"}]).output == "workers closed independently"
    assert all(not server.is_healthy for server in language_servers)
    assert sorted(len(server.requests) for server in language_servers) == [1, 2]


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_application_releases_lsp_created_before_startup_failure(platform_project, language_servers, monkeypatch, failure):
    from agentloom.adapters.lsp.lsp_server_manager import LSPServerInstance

    root, _, programs, _, run = platform_project
    source = root / "answer.py"
    source.write_text("answer = 42\n")
    enable_lsp(root)
    original_start = LSPServerInstance.start

    def fail_after_start(instance):
        original_start(instance)
        raise failure("Language server initialization interrupted after starting")

    monkeypatch.setattr(LSPServerInstance, "start", fail_after_start)

    def inspect(definition, request):
        result = definition.tool_gateway.invoke(call_id="hover", tool_name="lsp_hover", arguments={
            "file_path": str(source), "line": 1, "character": 1,
        })
        assert "requires a running language server" in result.output
        return "server unavailable"

    programs["platform"] = inspect
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            run(tools=[{"name": "lsp_hover"}])
    else:
        assert run(tools=[{"name": "lsp_hover"}]).output == "server unavailable"
    assert len(language_servers) == 1
    assert not language_servers[0].is_healthy


def test_parallel_worker_applications_own_mcp_connections_and_run_context(platform_project):
    root, workflow, programs, definitions, run = platform_project
    server = Path(__file__).parents[1] / "mcp_test" / "fixtures" / "stdio_server.py"
    events = root / "mcp-events.jsonl"
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"facts": {
        "command": sys.executable, "args": [str(server), str(events)],
    }}}))
    worker = workflow.parent / "worker_agents" / "probe.yaml"
    worker.parent.mkdir()
    worker.write_text(yaml.safe_dump({
        "name": "probe", "agent_runtime": "platform-fixture",
        "description": "Look up one fact.", "workflow": "Use the fact service.",
        "tools": [], "toolsets": [], "mcp_servers": str(config),
        "agent_function_schema": {
            "description": "Look up a fact.", "inputs": {"query": {"description": "Requested fact."}},
            "output": {"description": "The fact with its source identity."},
        },
    }))
    barrier = Barrier(2)
    observed = []

    def worker_program(definition, request):
        from agentloom.runtime.trace import capture_explicit_execution_context
        context = capture_explicit_execution_context()
        result = definition.tool_gateway.invoke(
            call_id=f"lookup-{definition.instance_id}", tool_name="mcp__facts__lookup",
            arguments={"query": request.task},
        )
        assert result.status == "completed", result.model_content()
        observed.append((definition.instance_id, context.root_run_id, context.local_run_id, result.output))
        barrier.wait(timeout=10)
        return json.dumps(result.output)

    def supervisor_program(definition, request):
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(
                copy_context().run, definition.tool_gateway.invoke,
                call_id=f"worker-{index}", tool_name="probe", arguments={"query": f"fact-{index}"},
            ) for index in range(2)]
            results = [call.result(timeout=20) for call in calls]
        assert all(result.status == "completed" for result in results), [result.model_content() for result in results]
        return "both workers completed"

    programs.update(platform=supervisor_program, probe=worker_program)
    assert run(worker_agents=[{"path": "probe.yaml"}]).output == "both workers completed"
    assert len(observed) == 2
    assert len({item[0] for item in observed}) == 2
    assert len({item[1] for item in observed}) == 1
    assert len({item[2] for item in observed}) == 2
    assert all(item[3]["calls"] == 1 for item in observed)
    pids = {item[3]["pid"] for item in observed}
    assert len(pids) == 2
    assert not any(psutil.pid_exists(pid) for pid in pids)
    started = [json.loads(line) for line in events.read_text().splitlines() if json.loads(line)["event"] == "started"]
    assert {item["pid"] for item in started} == pids


def test_goal_tools_complete_through_a_neutral_application(platform_project):
    _, _, programs, definitions, run = platform_project

    def complete(definition, request):
        gateway = definition.tool_gateway
        initial = gateway.invoke(call_id="goal-read", tool_name="get_goal", arguments={})
        assert json.loads(initial.output)["status"] == "active"
        completed = gateway.invoke(call_id="goal-complete", tool_name="update_goal", arguments={
            "status": "complete", "evidence": "Neutral Application read and verified the root Goal.",
        })
        assert completed.status == "completed", completed.model_content()
        return json.loads(completed.output)["status"]

    programs["platform"] = complete
    assert run(goal={"enabled": True}).output == "complete"
    assert {entry.provider for entry in definitions[0].tool_manifest} == {"agentloom"}


def test_skill_activation_uses_the_application_catalog_and_keeps_proposals_inactive(platform_project):
    root, workflow, programs, _, run = platform_project
    for scope, body in ((root / "skills", "project instructions"), (workflow.parents[1] / "skills", "application instructions")):
        directory = scope / "review"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: review\ndescription: Inspect a review.\n---\n{body}\n")

    def activate(definition, request):
        assert "Inspect a review." in definition.instructions
        assert "application instructions" not in definition.instructions
        selected = definition.tool_gateway.invoke(call_id="skill-read", tool_name="skill", arguments={"name": "review"})
        assert selected.status == "completed", selected.model_content()
        assert "application instructions" in selected.output
        assert "project instructions" not in selected.output
        proposal = definition.tool_gateway.invoke(call_id="skill-proposal", tool_name="skill_manage", arguments={
            "action": "create", "name": "future-review", "content": "# Future review\nUnreviewed candidate.\n",
        })
        assert proposal.status == "completed", proposal.model_content()
        value = json.loads(proposal.output)
        assert value["ok"] is True
        proposal_path = Path(value["proposal_path"])
        assert json.loads((proposal_path / "proposal.json").read_text())["status"] == "proposal"
        rejected = definition.tool_gateway.invoke(call_id="draft-activation", tool_name="skill", arguments={"name": "future-review"})
        assert rejected.status == "error"
        return "application skill active; proposal remains inactive"

    programs["platform"] = activate
    assert run(tools=[{"name": "skill"}, {"name": "skill_manage"}]).output == "application skill active; proposal remains inactive"


def test_memory_and_history_tools_use_existing_application_scope(platform_project):
    _, _, programs, _, run = platform_project

    def inspect_memory(definition, request):
        gateway = definition.tool_gateway
        listed = gateway.invoke(call_id="memory-list", tool_name="memory", arguments={"action": "list"})
        assert listed.status == "completed", listed.model_content()
        value = json.loads(listed.output)
        assert value["ok"] is True
        assert value["items"] == []
        history = gateway.invoke(call_id="history-search", tool_name="session_search", arguments={"query": "previous fact"})
        assert history.status == "completed", history.model_content()
        value = json.loads(history.output)
        assert value["ok"] is True
        assert value["scope"] == "current_app"
        assert value["app"] == "platform"
        promotion = gateway.invoke(call_id="memory-promote", tool_name="memory", arguments={
            "action": "propose", "scope": "project", "memory_key": "answer", "text": "The answer is 42.",
        })
        assert json.loads(promotion.output)["error"] == "project_promotion_requires_review"
        return "memory and history scope retained"

    programs["platform"] = inspect_memory
    assert run(tools=[{"name": "memory"}, {"name": "session_search"}], self_learning={"enabled": True}).output == "memory and history scope retained"


def test_native_application_retrieves_original_mcp_content_with_context_ref(platform_project):
    root, _, programs, _, run = platform_project
    system_path = root / "config" / "system.yaml"
    system = yaml.safe_load(system_path.read_text())
    system["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
    system_path.write_text(yaml.safe_dump(system))
    server = Path(__file__).parents[1] / "mcp_test" / "fixtures" / "stdio_server.py"
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"facts": {"command": sys.executable, "args": [str(server)]}}}))

    def retrieve(definition, request):
        from agentloom.runtime.context_engine.runtime import get_active_context_engine
        assert get_active_context_engine() is not None
        created = definition.tool_gateway.invoke(call_id="payload", tool_name="mcp__facts__context_payload", arguments={"query": "full artifact"})
        assert created.status == "completed", created.model_content()
        match = re.search(r"\[ContextRef (ctx_[a-zA-Z0-9]+)", created.output)
        assert match is not None, created.output[:300]
        ref = match.group(1)
        assert "PLATFORM-CTX-8426" not in created.output
        result = definition.tool_gateway.invoke(call_id="retrieve", tool_name="loom_retrieve_context", arguments={
            "ref": ref, "query": "TARGET_RECORD", "limit": 5,
        })
        assert result.status == "completed", result.model_content()
        assert "PLATFORM-CTX-8426" in result.output
        return "original artifact retrieved"

    programs["platform"] = retrieve
    assert run(
        tools=[{"name": "loom_retrieve_context"}], mcp_servers=str(config),
        context_engine={"min_chars": 1000, "preview_max_chars": 300},
    ).output == "original artifact retrieved"


@pytest.mark.parametrize("tool_name, arguments, marker", [
    ("get_file_outline", {"detail_level": "full"}, "compute_total"),
    ("ast_grep_search_file", {"keyword": "compute_total", "language": "python"}, "compute_total"),
    ("lsp_get_document_symbols", {"language": "python"}, "compute_total"),
])
def test_selected_code_analysis_tool_runs_in_neutral_application(platform_project, tool_name, arguments, marker):
    root, _, programs, _, run = platform_project
    source = root / "sample.py"
    source.write_text("def compute_total(values):\n    return sum(values)\n")

    def inspect(definition, request):
        assert {tool.name for tool in definition.tool_gateway.definitions} == {tool_name}
        result = definition.tool_gateway.invoke(call_id="inspect", tool_name=tool_name, arguments={
            "file_path": str(source), **arguments,
        })
        assert result.status == "completed", result.model_content()
        assert marker in str(result.output)
        return "selected code tool executed"

    programs["platform"] = inspect
    assert run(tools=[{"name": tool_name}]).output == "selected code tool executed"


def test_selected_markdown_tools_produce_real_artifacts_in_neutral_application(platform_project):
    root, _, programs, _, run = platform_project
    raw, structured = root / "raw.md", root / "structured.md"
    tools = ["write_markdown_file_raw", "write_markdown_file", "append_markdown_sections"]

    def write(definition, request):
        for index, (tool_name, arguments) in enumerate([
            (tools[0], {"file_path": str(raw), "content_plain": "# Original\n\nKeep this.\n"}),
            (tools[1], {"file_path": str(structured), "title": "Report", "sections": [
                {"heading": "Evidence", "level": 2, "body": "Verified facts."},
            ]}),
            (tools[2], {"file_path": str(raw), "sections": [
                {"heading": "Appendix", "level": 2, "body": "Additional facts."},
            ]}),
        ]):
            result = definition.tool_gateway.invoke(call_id=f"write-{index}", tool_name=tool_name, arguments=arguments)
            assert result.status == "completed", result.model_content()
        return "reports written"

    programs["platform"] = write
    assert run(tools=[{"name": name} for name in tools]).output == "reports written"
    assert all(marker in raw.read_text() for marker in ("# Original", "Keep this.", "## Appendix", "Additional facts."))
    assert all(marker in structured.read_text() for marker in ("# Report", "## Evidence", "Verified facts."))
