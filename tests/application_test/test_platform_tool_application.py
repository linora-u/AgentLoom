"""Platform tools at the real YAML Application seam with a neutral runtime."""

import json
import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Barrier

import psutil
import pytest
import yaml
from agentloom.app.run import ApplicationRunInterrupted
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.agent_runtime import AgentRuntimeResult, RuntimeCapabilities, RuntimeCheckpointEnvelope


@pytest.fixture
def platform_project(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.yaml").write_text(yaml.safe_dump({
        "checkpoint": {"enabled": False}, "self_learning": {"enabled": False},
        "default_toolsets": [], "smart_summary": False,
        "logging": {"console_enabled": False},
        "runtime": {"root_dir": "state/runtime"},
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
    workflow.write_text(yaml.safe_dump(definition))
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

    from agentloom.app.composition import build_builtin_runtime_registry
    registry = build_builtin_runtime_registry()
    registry.register("platform-fixture", capabilities=PlatformRuntime.capabilities, factory=PlatformRuntime)
    monkeypatch.setattr("agentloom.app.validation.build_builtin_runtime_registry", lambda: registry)
    monkeypatch.setattr("agentloom.app.agent.build_builtin_runtime_registry", lambda: registry)

    def run(*, resume_task_id=None, **updates):
        workflow.write_text(yaml.safe_dump({**definition, **updates}))
        with bind_config(load_project_config(tmp_path)):
            return execute_app(
                workflow,
                file_logging=False,
                resume_task_id=resume_task_id,
            )

    return tmp_path, workflow, programs, definitions, run


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
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Requested fact."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }))
    barrier = Barrier(2)
    observed = []

    def worker_program(definition, request):
        from agentloom.execution.trace import capture_explicit_execution_context
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


def test_application_uses_one_canonical_runtime_home(platform_project):
    root, workflow, programs, _, run = platform_project
    runtime_root = root / "state" / "runtime"
    hook_evidence = workflow.parents[1] / "outputs" / "hook-events.jsonl"
    hook_script = root / "hooks" / "capture_runtime_paths.py"
    hook_script.parent.mkdir()
    hook_script.write_text(
        """\
import json
import sys
from pathlib import Path

payload = json.load(sys.stdin)
target = Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "tool_name": payload["tool_name"],
        "agent_task_workspace": payload["agent_task_workspace"],
        "agent_insights_path": payload["agent_insights_path"],
    }) + "\\n")
json.dump({"decision": "allow"}, sys.stdout)
""",
        encoding="utf-8",
    )
    system_path = root / "config" / "system.yaml"
    system = yaml.safe_load(system_path.read_text(encoding="utf-8"))
    system.update(
        checkpoint={"enabled": True, "cleanup_on_success": False},
        self_learning={"enabled": True},
        hooks={
            "PreToolUse": [
                {
                    "id": "capture-runtime-paths",
                    "matcher": "*",
                    "command": shlex.join(
                        [sys.executable, str(hook_script), str(hook_evidence)]
                    ),
                }
            ]
        },
    )
    system_path.write_text(yaml.safe_dump(system), encoding="utf-8")

    skill = workflow.parents[1] / "skills" / "storage-proof"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: storage-proof\ndescription: Verify canonical runtime storage.\n---\n"
        "Use the canonical runtime evidence.\n",
        encoding="utf-8",
    )
    worker = workflow.parent / "worker_agents" / "storage_worker.yaml"
    worker.parent.mkdir()
    worker.write_text(
        yaml.safe_dump(
            {
                "name": "storage_worker",
                "agent_runtime": "platform-fixture",
                "description": "Read Application memory in an isolated Worker.",
                "workflow": "List Application memory and return.",
                "tools": [{"name": "memory"}],
                "toolsets": [],
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Verification request.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ),
        encoding="utf-8",
    )

    def worker_program(definition, request):
        observed = definition.tool_gateway.invoke(
            call_id="worker-memory",
            tool_name="memory",
            arguments={"action": "list", "scope": "app"},
        )
        assert observed.status == "completed", observed.model_content()
        return "worker storage verified"

    def supervisor_program(definition, request):
        if request.checkpoint is not None:
            assert request.continue_session is True
            assert request.checkpoint.payload == {"phase": "ready-to-resume"}
        activated = definition.tool_gateway.invoke(
            call_id="activate-skill",
            tool_name="skill",
            arguments={"name": "storage-proof"},
        )
        assert activated.status == "completed", activated.model_content()
        assert "canonical runtime evidence" in activated.output
        memory = definition.tool_gateway.invoke(
            call_id="supervisor-memory",
            tool_name="memory",
            arguments={"action": "list", "scope": "app"},
        )
        assert memory.status == "completed", memory.model_content()
        delegated = definition.tool_gateway.invoke(
            call_id="delegate-storage",
            tool_name="storage_worker",
            arguments={"query": "verify storage"},
        )
        assert delegated.status == "completed", delegated.model_content()
        assert delegated.output == "worker storage verified"
        if request.checkpoint is None:
            assert request.checkpoint_sink is not None
            request.checkpoint_sink(
                RuntimeCheckpointEnvelope(
                    runtime_id="platform-fixture",
                    runtime_version="fixture",
                    state_schema_version=1,
                    payload={"phase": "ready-to-resume"},
                    task_id=request.task_id,
                    run_id=request.run_id,
                    progress=1,
                )
            )
            raise KeyboardInterrupt("simulate an interrupted Application")
        return "canonical storage verified"

    programs.update(platform=supervisor_program, storage_worker=worker_program)
    run_config = {
        "tools": [
            {"name": "skill"},
            {"name": "memory"},
        ],
        "worker_agents": [{"path": "storage_worker.yaml"}],
    }
    files_before_run = {path for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ApplicationRunInterrupted) as interrupted:
        run(**run_config)

    first_run = interrupted.value.run
    result = run(resume_task_id=first_run.task_id, **run_config)

    assert result.output == "canonical storage verified"
    assert result.run.task_id == first_run.task_id
    assert result.run.run_id != first_run.run_id
    assert result.run.run_dir != first_run.run_dir
    assert first_run.run_dir.is_relative_to(runtime_root / "runs" / "platform")
    assert result.run.run_dir.is_relative_to(runtime_root / "runs" / "platform")
    assert first_run.manifest_path.is_file()
    assert result.run.manifest_path.is_file()
    checkpoint_dir = (
        runtime_root
        / "checkpoints"
        / result.run.application_id
        / result.run.task_id
    )
    assert checkpoint_dir.is_dir()
    assert (runtime_root / "self_learning.db").is_file()
    assert (result.run.run_dir / "artifacts" / "result.txt").read_text(
        encoding="utf-8"
    ) == result.output
    for run_dir in (first_run.run_dir, result.run.run_dir):
        skill_artifacts = list((run_dir / "artifacts" / "skills").glob("*.md"))
        assert skill_artifacts
        assert "canonical runtime evidence" in skill_artifacts[-1].read_text(
            encoding="utf-8"
        )
        assert (run_dir / "audit" / "task_events.jsonl").is_file()
    assert hook_evidence.is_file()
    hook_records = [
        json.loads(line)
        for line in hook_evidence.read_text(encoding="utf-8").splitlines()
    ]
    assert {record["tool_name"] for record in hook_records} >= {
        "skill",
        "memory",
        "storage_worker",
    }
    task_workspaces = {
        Path(record["agent_task_workspace"])
        for record in hook_records
        if record["agent_task_workspace"]
    }
    assert len(task_workspaces) >= 2
    assert all(
        path.is_dir()
        and path.is_relative_to(runtime_root / "workspaces" / "agents" / "platform")
        for path in task_workspaces
    )
    assert hook_evidence.is_relative_to(root / "applications" / "platform" / "outputs")
    assert not hook_evidence.is_relative_to(runtime_root)
    files_created_by_run = {
        path for path in root.rglob("*") if path.is_file()
    } - files_before_run
    assert files_created_by_run
    unexpected_files = {
        path
        for path in files_created_by_run
        if not path.is_relative_to(runtime_root)
        and not path.is_relative_to(root / "applications" / "platform" / "outputs")
    }
    assert unexpected_files == set()
    assert not (root / ".runtime").exists()
    assert not (root / ".logs").exists()


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
        from agentloom.execution.context_engine.runtime import get_active_context_engine
        assert get_active_context_engine() is not None
        created = definition.tool_gateway.invoke(call_id="payload", tool_name="mcp__facts__context_payload", arguments={"query": "full artifact"})
        assert created.status == "completed", created.model_content()
        model_content = created.model_content()
        match = re.search(r"\[ContextRef (ctx_[a-zA-Z0-9]+)", model_content)
        assert match is not None, model_content[:300]
        ref = match.group(1)
        assert "PLATFORM-CTX-8426" not in model_content
        assert "PLATFORM-CTX-8426" in created.output
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


def trace_payload(value: str) -> str:
    """Return the exact text supplied by an Application Tool call."""
    return value


def test_application_tool_outcome_has_durable_inspectable_payload(platform_project):
    from agentloom.execution.observability import inspect_run

    _, _, programs, _, run = platform_project
    payload = "application-trace-8426\n" * 1000

    def execute(definition, _request):
        record = definition.tool_gateway.invoke(
            call_id="trace-call", tool_name="trace_payload", arguments={"value": payload},
        )
        assert record.status == "completed"
        return "trace complete"

    programs["platform"] = execute
    result = run(tools=[{"name": "trace_payload", "module": __name__, "function": "trace_payload"}])

    with inspect_run(result.run) as trace:
        calls = [event for event in trace.events() if event["kind"] == "tool"]
        assert len(calls) == 1
        assert calls[0]["call_id"] == "trace-call"
        assert calls[0]["status"] == "completed"
        assert json.loads(trace.read_text(calls[0]["input_ref"])) == {"value": payload}
        assert json.loads(trace.read_text(calls[0]["output_ref"])) == payload
        model_visible = trace.read_text(calls[0]["model_ref"])
        match = re.search(r"\[ContextRef (ctx_[0-9a-f]{32})", model_visible)
        assert match is not None
        assert trace.read_text(match.group(1)) == payload
        pages = []
        offset = 0
        while True:
            page = trace.read_page(calls[0]["model_ref"], offset=offset, limit=1024)
            pages.append(page.data)
            if page.next_offset is None:
                break
            offset = page.next_offset
        assert b"".join(pages).decode() == model_visible


def test_application_fails_when_required_tool_trace_cannot_be_written(platform_project, monkeypatch):
    from agentloom.app.run import ApplicationRunError
    from agentloom.execution.observability import TraceRecorder

    _, _, programs, _, run = platform_project
    def execute(definition, _request):
        definition.tool_gateway.invoke(
            call_id="trace-failure", tool_name="trace_payload", arguments={"value": "committed"},
        )
        return "must not succeed"

    original_append = TraceRecorder._append

    def fail_write(recorder, event):
        if event["kind"] == "tool":
            raise OSError("fixture disk full")
        return original_append(recorder, event)

    programs["platform"] = execute
    monkeypatch.setattr(TraceRecorder, "_append", fail_write)
    with pytest.raises(ApplicationRunError, match="Could not persist Tool trace"):
        run(tools=[{"name": "trace_payload", "module": __name__, "function": "trace_payload"}])


def test_application_trace_records_effective_pre_tool_decision(platform_project):
    from agentloom.execution.observability import inspect_run

    root, _, programs, _, run = platform_project
    hook = root / "modify_tool.py"
    hook.write_text('import json\nprint(json.dumps({"decision":"modify","modified_input":{"value":"after"}}))\n')

    def execute(definition, _request):
        record = definition.tool_gateway.invoke(
            call_id="modified-call", tool_name="trace_payload", arguments={"value": "before"},
        )
        assert record.status == "completed"
        return record.output

    programs["platform"] = execute
    result = run(
        tools=[{"name": "trace_payload", "module": __name__, "function": "trace_payload"}],
        hooks={"PreToolUse": [{"id": "modify", "matcher": "trace_payload",
                               "command": shlex.join([sys.executable, str(hook)])}]},
    )
    assert result.output == "after"
    with inspect_run(result.run) as trace:
        decisions = [event for event in trace.events() if event["kind"] == "hook_decision"]
        assert len(decisions) == 1
        assert decisions[0]["event"] == "PreToolUse"
        assert decisions[0]["call_id"] == "modified-call"
        detail = json.loads(trace.read_text(decisions[0]["decision_ref"]))
        assert detail["result"]["decision"] == "modify"
        assert detail["result"]["modified_input"] == {"value": "after"}


def test_large_tool_reference_survives_context_cache_eviction(platform_project):
    _, _, programs, _, run = platform_project

    def execute(definition, _request):
        first = definition.tool_gateway.invoke(
            call_id="first-large", tool_name="trace_payload",
            arguments={"value": "FIRST-RECORD-8426\n" + "a" * 5000},
        )
        second = definition.tool_gateway.invoke(
            call_id="second-large", tool_name="trace_payload",
            arguments={"value": "SECOND-RECORD-8426\n" + "b" * 5000},
        )
        assert second.status == "completed"
        match = re.search(r"\[ContextRef (ctx_[0-9a-f]+)", first.model_content())
        assert match is not None
        retrieved = definition.tool_gateway.invoke(
            call_id="fetch-first", tool_name="loom_retrieve_context",
            arguments={"ref": match.group(1), "offset": 0, "limit": 4096},
        )
        assert retrieved.status == "completed"
        return retrieved.model_content()

    programs["platform"] = execute
    result = run(
        tools=[
            {"name": "trace_payload", "module": __name__, "function": "trace_payload"},
            {"name": "loom_retrieve_context"},
        ],
        context_engine={"min_chars": 1000, "preview_max_chars": 100,
                        "store": {"max_entries": 1}},
    )
    assert "FIRST-RECORD-8426" in result.output


def test_durable_tool_reference_pages_utf8_content_without_loss(platform_project):
    _, _, programs, _, run = platform_project
    payload = "汉字🙂" * 450

    def execute(definition, _request):
        created = definition.tool_gateway.invoke(
            call_id="utf8-large", tool_name="trace_payload", arguments={"value": payload},
        )
        match = re.search(r"\[ContextRef (ctx_[0-9a-f]{32})", created.model_content())
        assert match is not None
        offset = 0
        pieces = []
        while True:
            retrieved = definition.tool_gateway.invoke(
                call_id=f"page-{offset}", tool_name="loom_retrieve_context",
                arguments={"ref": match.group(1), "offset": offset, "limit": 127},
            )
            assert retrieved.status == "completed"
            header, body = retrieved.output.split("\n", 1)
            pieces.append(body)
            next_match = re.search(r"next_offset=(\d+|none)", header)
            assert next_match is not None
            if next_match.group(1) == "none":
                break
            offset = int(next_match.group(1))
        return "".join(pieces)

    programs["platform"] = execute
    result = run(
        tools=[
            {"name": "trace_payload", "module": __name__, "function": "trace_payload"},
            {"name": "loom_retrieve_context"},
        ],
        context_engine={"min_chars": 1000, "preview_max_chars": 100},
    )
    assert result.output == payload
