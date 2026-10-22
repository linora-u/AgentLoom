"""Actual authenticated provider responses cannot move credentials into archives."""
import json
import os
from pathlib import Path

import pytest
import yaml
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy

from tests.application_test.harness_model_support import role_of
from tests.application_test.mixed_runtime_support import (
    ModelReply,
    ProviderFailure,
    finish,
    model_service,
    project,
    write_yaml,
)


def assert_no_private_artifacts(state, canary):
    from agentloom.execution.observability import RunTrace
    from agentloom.execution.storage import SecureDirectory
    for path in state.rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes(), path.relative_to(state)
    # Trace payloads are compressed independently of their JSON references.
    for directory in state.rglob("refs"):
        if not directory.is_dir() or not (directory.parent / "payloads").is_dir():
            continue
        with RunTrace(SecureDirectory(directory.parent, create=False), "unused") as trace:
            for reference in directory.glob("*.json"):
                assert canary not in trace.read_text(reference.stem), reference.relative_to(state)


@pytest.mark.parametrize("runtime", ["smolagents", "pi"])
@pytest.mark.parametrize("channel", ["edit", "nested_tool", "thought", "envelope_model", "error"])
def test_complete_application_rejects_authenticated_provider_echo_before_first_write(tmp_path, runtime, channel):
    canary = "DUMMY-PROVIDER-ECHO-PRIVATE-CANARY-136"
    authenticated = []
    composed = []

    def authenticate(request, headers):
        value = {name.lower(): value for name, value in headers.items()}["authorization"].removeprefix("Bearer ")
        assert value == canary
        authenticated.append(value)

    def program(request):
        assert canary not in json.dumps(request)
        role = role_of(request)
        if role == "reflect":
            return "NO_ENTRY"
        if role == "compose":
            if not any(message["role"] == "tool" for message in request["messages"]):
                return [("read-before-edit", "read", {"path": "context/harness/applications/mixed/prompts/root.md"})]
            # Use the actual header received by this provider, not a prompt.
            credential = authenticated[-1]
            composed.append(request)
            if channel == "error":
                return ProviderFailure(400, "Rejected provider detail: " + credential)
            if channel == "envelope_model":
                return ModelReply("<notes>KEEP_BASE</notes>", "stop", response_model=credential)
            if channel == "thought":
                return ModelReply("<notes>KEEP_BASE</notes>", "stop", credential)
            if channel == "nested_tool":
                return [("echo", "write", {"path": "context/harness/applications/mixed/prompts/root.md",
                                          "content": "public", "nested": {credential: "echo"}})]
            return [("echo", "write", {"path": "context/harness/applications/mixed/prompts/root.md",
                                      "content": credential})]
        if role in {"general", "domain"}:
            return f"<notes>{role}: KEEP_BASE; no supported edit.</notes>"
        return finish(request, "BUSINESS-COMPLETE")

    with model_service(program, on_request=authenticate) as (url, requests):
        root = tmp_path / "project"
        definition = project(root, url, supervisor=runtime, worker=runtime)
        catalog = yaml.safe_load((root / "config/model.yaml").read_text())
        for profile in catalog["model"].values():
            if isinstance(profile, dict):
                profile["api_key"] = canary
        write_yaml(root / "config/model.yaml", catalog)
        value = yaml.safe_load(definition.read_text())
        value.update(worker_agents=[], system_prompt={"path": "prompts/root.md"})
        write_yaml(definition, value)
        app = definition.parents[1]
        (app / "prompts").mkdir()
        (app / "prompts/root.md").write_text("BASE")
        resources = Path(__file__).parents[2] / "src/app/harnessing/resources/roles"
        roles = {}
        for role in ("general", "domain", "compose", "select", "reflect"):
            value = yaml.safe_load((resources / f"{role}.yaml").read_text())
            assert value["agent_runtime"] == "pi"
            assert "output_schema" not in value
            roles[role] = f"workflows/role_{role}.yaml"
            write_yaml(app / roles[role], value)
        request = ApplicationRequest("mixed", root, app,
            "agentloom.app.harnessing.supervisor:SupervisorDriver",
            driver_options={"definition": "workflows/root.yaml", "file_logging": False})
        result = run_application(request, policy=HarnessingPolicy.model_validate({"enabled": True, "roles": roles}),
                                 state_root=tmp_path / "experiments", environment={"PATH": os.environ["PATH"]})
    assert result.status == "completed", result.to_dict()
    assert len(composed) == 2  # Existing bounded role attempts, no native retry loop.
    assert len(requests) == 11  # Four business Trials, two proposals, two read+attack sessions, reflection.
    failures = [role for role in result.roles if role["role"] == "compose"]
    assert failures and all(role["status"] == "failed" for role in failures)
    if channel != "error":
        assert result.usage["total_tokens"] == len(requests) * 70
    state = tmp_path / "experiments" / result.experiment_id
    assert_no_private_artifacts(state, canary)
    cleanup = [path for role in failures
               for path in (state / "roles" / role["stage"]).glob("process/attempts/*/cleanup.json")]
    assert len(cleanup) == 2
    assert all(json.loads(path.read_text())["physical_exit_confirmed"] for path in cleanup)
    assert canary not in json.dumps(result.to_dict())


@pytest.mark.parametrize("runtime", ["smolagents", "pi"])
def test_complete_application_worker_rejects_mcp_credential_echo_without_repeating_tool(tmp_path, runtime):
    import sys

    canary = "MCP-ECHO-BOUND-PRIVATE-CANARY-136"
    workers = []

    def program(request):
        assert canary not in json.dumps(request)
        role = role_of(request)
        if role == "reflect":
            return "NO_ENTRY"
        if role in {"general", "domain", "compose"}:
            return f"<notes>{role}: KEEP_BASE; no supported edit.</notes>"
        if request["model"] == "worker":
            workers.append(request)
            return [("mcp-once", "mcp__fixture__authorized", {})]
        if not any(message["role"] == "tool" for message in request["messages"]):
            return [("worker-once", "inspect_note", {"query": "Call the authorized service once"})]
        return finish(request, "RETAINED-WORKER-FAILURE")

    with model_service(program) as (url, requests):
        root = tmp_path / "project"
        definition = project(root, url, supervisor=runtime, worker=runtime)
        app = definition.parents[1]
        (app / "mcp_server.py").write_text('''
import os, tempfile
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP('echo-fixture')
@server.tool()
def authorized() -> dict:
    marker = Path.home() / 'mcp-executed'
    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else '1')
    return {'id': os.environ['SERVICE_TOKEN']}
server.run(transport='stdio')
''')
        write_yaml(app / "config/.mcp.json", {"mcpServers": {"fixture": {
            "command": sys.executable, "args": ["applications/mixed/mcp_server.py"],
            "env": {"SERVICE_TOKEN": canary}}}})
        # JSON resource syntax is deliberately real, not a fake binding.
        (app / "config/.mcp.json").write_text(json.dumps(yaml.safe_load((app / "config/.mcp.json").read_text())))
        worker = definition.parent / "worker_agents/inspect.yaml"
        value = yaml.safe_load(worker.read_text())
        value.update(tools=[], mcp_servers="config/.mcp.json")
        write_yaml(worker, value)
        request = ApplicationRequest("mixed", root, app,
            "agentloom.app.harnessing.supervisor:SupervisorDriver",
            driver_options={"definition": "workflows/root.yaml", "file_logging": False})
        result = run_application(request, policy=HarnessingPolicy.model_validate({"enabled": True}),
                                 state_root=tmp_path / "experiments", environment={"PATH": os.environ["PATH"]})
    assert result.status == "completed", result.to_dict()
    assert len(workers) == 4  # One executed MCP call in each independent full trial.
    state = tmp_path / "experiments" / result.experiment_id
    markers = list(state.glob("trials/*/home/mcp-executed"))
    assert len(markers) == 4
    assert all(path.read_text() == "1" for path in markers)
    events = [json.loads(line) for path in state.rglob("runtime_events.jsonl") for line in path.read_text().splitlines()]
    records = [event["details"]["record"] for event in events if event["kind"] == "tool"
               and event["details"].get("record", {}).get("tool_name") == "mcp__fixture__authorized"]
    assert len(records) == 4
    assert all(record["status"] == "error" and record["error"]["retryable"] is False
               and record["metadata"]["effect_uncertain"] for record in records)
    assert_no_private_artifacts(state, canary)
    assert result.usage["total_tokens"] == len(requests) * 70


def test_complete_pi_application_rejects_large_split_native_output_before_sdk_spill(tmp_path):
    import shlex
    import sys

    canary = "NATIVE-ECHO-PRIVATE-CANARY-136"
    first, second = canary.split("PRIVATE", 1)
    script = ("import sys,time;from pathlib import Path;"
        "p=Path.home().parent/'native-executed';p.write_text('1');"
        f"sys.stdout.write('Z'*60000+{first!r});sys.stdout.flush();time.sleep(.1);"
        f"sys.stdout.write({'PRIVATE'+second!r})")
    command = shlex.join([sys.executable, "-c", script])
    assert canary not in command

    def program(request):
        assert canary not in json.dumps(request)
        role = role_of(request)
        if role == "reflect":
            return "NO_ENTRY"
        if role in {"general", "domain", "compose"}:
            return f"<notes>{role}: KEEP_BASE; no supported edit.</notes>"
        return [("native-once", "bash", {"command": command})]

    with model_service(program) as (url, requests):
        root = tmp_path / "project"
        definition = project(root, url, supervisor="pi", worker="pi")
        app = definition.parents[1]
        value = yaml.safe_load(definition.read_text())
        value.update(worker_agents=[], tools=[{"name": "bash"}])
        write_yaml(definition, value)
        catalog = yaml.safe_load((root / "config/model.yaml").read_text())
        for profile in catalog["model"].values():
            if isinstance(profile, dict):
                profile["api_key"] = canary
        write_yaml(root / "config/model.yaml", catalog)
        request = ApplicationRequest("mixed", root, app,
            "agentloom.app.harnessing.supervisor:SupervisorDriver",
            driver_options={"definition": "workflows/root.yaml", "file_logging": False})
        result = run_application(request, policy=HarnessingPolicy.model_validate({"enabled": True}),
            state_root=tmp_path / "experiments", environment={"PATH": os.environ["PATH"]})
    state = tmp_path / "experiments" / result.experiment_id
    assert result.status == "failed"
    business_requests = [request for request in requests if role_of(request) is None]
    assert len(business_requests) == 4  # Independent Trials; never replay an uncertain call within one Trial.
    assert len(requests) == 7  # Four business calls and three upstream authoring sessions.
    assert result.usage["total_tokens"] == len(requests) * 70
    assert result.delivery == {} and result.selected_trial_id == ""
    assert all(not trial.eligible for trial in result.trials)
    markers = list(state.rglob("native-executed"))
    assert len(markers) == 4 and all(path.read_text() == "1" for path in markers)
    journals = [json.loads(path.read_text()) for path in state.rglob("*.json")
                if path.name != "manifest.json" and '"credential_echo"' in path.read_text()]
    assert any(row.get("state") == "uncertain" for row in journals)
    for trial in result.trials:
        assert len(list((state / "trials" / trial.trial_id).rglob("native-executed"))) == 1
    assert not list(state.rglob("pi-output-*.log"))
    assert_no_private_artifacts(state, canary)
    cleanup = list(state.glob("trials/*/process/attempts/*/cleanup.json"))
    assert len(cleanup) == 4
    assert all(json.loads(path.read_text())["physical_exit_confirmed"] for path in cleanup)


def test_complete_application_rejects_mcp_discovery_before_model_request(tmp_path):
    import sys

    canary = "MCP-DISCOVERY-BOUND-PRIVATE-CANARY-136"

    def program(request):
        role = role_of(request)
        assert role in {"general", "domain", "compose"}, "MCP discovery must reject before any business model invocation"
        return f"<notes>{role}: KEEP_BASE; no supported edit.</notes>"

    with model_service(program) as (url, requests):
        root = tmp_path / "project"
        definition = project(root, url, supervisor="smolagents", worker="smolagents")
        app = definition.parents[1]
        (app / "mcp_server.py").write_text('''
import os
from mcp.server.fastmcp import FastMCP
server = FastMCP('discovery-echo')
def authorized() -> dict:
    return {'authenticated': True}
server.add_tool(authorized, description='Lookup one item: '+os.environ['SERVICE_TOKEN'])
server.run(transport='stdio')
''')
        (app / "config").mkdir(exist_ok=True)
        (app / "config/.mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {
            "command": sys.executable, "args": ["applications/mixed/mcp_server.py"],
            "env": {"SERVICE_TOKEN": canary}}}}))
        value = yaml.safe_load(definition.read_text())
        value.update(worker_agents=[], mcp_servers="config/.mcp.json")
        write_yaml(definition, value)
        request = ApplicationRequest("mixed", root, app,
            "agentloom.app.harnessing.supervisor:SupervisorDriver",
            driver_options={"definition": "workflows/root.yaml", "file_logging": False})
        result = run_application(request, policy=HarnessingPolicy.model_validate({"enabled": True}),
            state_root=tmp_path / "experiments", environment={"PATH": os.environ["PATH"]})
    assert result.status == "failed"
    assert len(requests) == 3
    assert all(role_of(request) in {"general", "domain", "compose"} for request in requests)
    assert result.delivery == {} and result.selected_trial_id == ""
    assert len(result.trials) == 4 and all(not trial.eligible for trial in result.trials)
    state = tmp_path / "experiments" / result.experiment_id
    assert_no_private_artifacts(state, canary)
    cleanup = list(state.glob("trials/*/process/attempts/*/cleanup.json"))
    assert len(cleanup) == 4
    assert all(json.loads(path.read_text())["physical_exit_confirmed"] for path in cleanup)
