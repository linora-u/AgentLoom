"""Minimal model dispatch facts do not require retained Model bodies."""

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.agent_runtime import AgentRuntimeError
from agentloom.execution.native_tools import NativeCallIdentity, NativePrepareRequest, ToolManifestEntry
from agentloom.execution.subprocess_env import build_subprocess_env
from agentloom.runtimes.pi.install import find_node, installed_pi_entry
from agentloom.runtimes.pi.protocol import (
    ModelPrepare,
    ModelResponse,
    ModelToolCall,
    ModelTrace,
    PlatformPrepare,
    Prepare,
)
from agentloom.runtimes.pi.protocol_handlers import PiModelHandler, PiNativeToolHandler, PiPlatformToolHandler
from pydantic import ValidationError

from tests.pi_test.test_application import model_service, project


def model_handler(tmp_path):
    identity = NativeCallIdentity("app", "task", "run", "instance", "model:one", "session", "parent")
    handler = PiModelHandler(request=SimpleNamespace(application_id="app", task_id="task"),
        run_id="run", instance_id="instance", private_directory=tmp_path,
        execution=SimpleNamespace(local_run_id="agent"), hook=None, shared_goal=None)
    return identity, handler


def test_response_context_is_available_without_trace_and_rejects_replay(tmp_path):
    identity, handler = model_handler(tmp_path)
    permit = handler.handle(ModelPrepare(method="model_prepare", identity=identity))
    assert permit.trace_enabled is False
    response = ModelResponse(method="model_response", identity=identity, attempt=0,
        tool_calls=[ModelToolCall(id="first", name="revise"), ModelToolCall(id="second", name="finish")])
    assert handler.response(response).accepted
    assert handler.tool_call_context(replace(identity, call_id="first"), "revise").assistant_tools == ("revise", "finish")
    assert handler.tool_call_context(replace(identity, call_id="second"), "finish").assistant_tools == ("revise", "finish")
    assert not list(tmp_path.iterdir())
    with pytest.raises(AgentRuntimeError, match="Duplicate"):
        handler.response(response)
    with pytest.raises(AgentRuntimeError, match="Duplicate"):
        handler.response(response.model_copy(update={"attempt": 1, "tool_calls": [ModelToolCall(id="third", name="other")]}))
    with pytest.raises(AgentRuntimeError, match="prepared"):
        handler.response(response.model_copy(update={"identity": replace(identity, call_id="model:unknown")}))
    with pytest.raises(AgentRuntimeError, match="prepared"):
        handler.response(response.model_copy(update={"identity": replace(identity, native_parent_id="other")}))
    with pytest.raises(AgentRuntimeError, match="identity"):
        handler.response(response.model_copy(update={"identity": replace(identity, task_id="other")}))


@pytest.mark.parametrize("owner", ["platform", "runtime"])
def test_dispatch_cannot_use_another_tools_call_id(tmp_path, owner):
    identity, model = model_handler(tmp_path)
    model.handle(ModelPrepare(method="model_prepare", identity=identity))
    model.response(ModelResponse(method="model_response", identity=identity, attempt=0,
        tool_calls=[ModelToolCall(id="first", name="allowedA"), ModelToolCall(id="second", name="allowedB")]))
    called = []
    entry = ToolManifestEntry(logical_name="allowedB", visible_name="allowedB", owner=owner,
        provider="fixture", capability="fixture.read", operation="read",
        parameters={"type": "object", "properties": {}})
    entries = {"allowedA": replace(entry, logical_name="allowedA", visible_name="allowedA"), "allowedB": entry}
    first = replace(identity, call_id="first")
    if owner == "platform":
        gateway = SimpleNamespace(prepare=lambda **arguments: called.append(arguments))
        handler = PiPlatformToolHandler(request=SimpleNamespace(application_id="app", task_id="task"),
            definition=SimpleNamespace(tool_gateway=gateway), run_id="run", instance_id="instance",
            platform_entries=entries, checkpoint=SimpleNamespace(store=None),
            record_tool=lambda *args: None, call_context=model.tool_call_context)
        payload = PlatformPrepare(method="platform_prepare", identity=first, tool_name="allowedB", arguments={})
    else:
        handler = PiNativeToolHandler(native=SimpleNamespace(prepare=lambda call: called.append(call)),
            native_entries=tuple(entries.values()), private_directory=tmp_path, record_tool=lambda *args: None,
            call_context=model.tool_call_context)
        payload = Prepare(method="tool_prepare", call=NativePrepareRequest(first, entry, str(tmp_path), {}))
    with pytest.raises(AgentRuntimeError, match="tool name does not match"):
        handler.prepare(payload)
    assert called == []


def test_response_wire_rejects_tool_bodies_and_duplicate_ids(tmp_path):
    identity, _ = model_handler(tmp_path)
    arguments = {"method": "model_response", "identity": identity, "attempt": 0,
                 "tool_calls": [{"id": "one", "name": "probe", "arguments": {"secret": "internal"}}]}
    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelResponse.model_validate(arguments)
    arguments["tool_calls"] = [{"id": "one", "name": "probe"}, {"id": "one", "name": "finish"}]
    with pytest.raises(ValidationError, match="unique"):
        ModelResponse.model_validate(arguments)


def test_disabled_trace_rejects_body_capture_without_reading_file(tmp_path):
    identity, handler = model_handler(tmp_path)
    handler.handle(ModelPrepare(method="model_prepare", identity=identity))
    with pytest.raises(AgentRuntimeError, match="tracing is disabled"):
        handler.trace(ModelTrace(method="model_trace", identity=identity, attempt=0,
            phase="request", capture_id="a" * 32, sha256="b" * 64))


def test_trace_off_bridge_sends_only_dispatch_facts_and_no_capture_files(tmp_path):
    script = """
import {readdir} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
const {configureModel} = await import(pathToFileURL(process.argv[1]).href);
const directory = process.argv[2];
const calls = [];
const model = {api: 'openai-completions', provider: 'agentloom', id: 'fixture'};
let outgoing;
const session = {retryAttempt: 0, agent: {streamFunction: async (_model, _context, options) => {
  outgoing = await options.onPayload({model: 'fixture', messages: [{content: 'private input'}]}, model);
  return {result: async () => ({role: 'assistant', content: [
    {type: 'text', text: 'private model body'},
    {type: 'toolCall', id: 'tool-one', name: 'probe', arguments: {private: 'arguments'}}], stopReason: 'toolUse'})};
}}};
configureModel(session, {max_output_tokens: 100, extra_completion_params: {}}, {},
  async () => ({state: 'work', agent_context: [], trace_enabled: false, identity: {call_id: 'model:one'}}),
  directory, async payload => {
    calls.push(payload);
    return {method: 'model_response', identity: payload.identity, attempt: payload.attempt, accepted: true};
  });
const stream = await session.agent.streamFunction(model, {messages: []}, {});
const result = await stream.result();
console.log(JSON.stringify({calls, files: await readdir(directory), cap: outgoing.max_tokens,
  stopReason: result.stopReason}));
"""
    result = subprocess.run([find_node(build_subprocess_env()), "--input-type=module", "-e", script,
        str(installed_pi_entry().with_name("model.js")), str(tmp_path)],
        capture_output=True, text=True, check=True, timeout=15)
    observed = json.loads(result.stdout)
    assert observed == {"calls": [{"method": "model_response", "identity": {"call_id": "model:one"},
        "attempt": 0, "tool_calls": [{"id": "tool-one", "name": "probe"}]}],
        "files": [], "cap": 100, "stopReason": "toolUse"}


def test_real_pi_trace_off_reports_facts_without_model_files(tmp_path, monkeypatch):
    observed = []
    response = PiModelHandler.response

    def receive(handler, payload):
        from agentloom.execution.observability import get_current_trace_recorder

        assert get_current_trace_recorder() is None
        assert not list(handler._private_directory.rglob("model-*.json"))
        observed.append(payload.model_dump())
        return response(handler, payload)

    monkeypatch.setattr(PiModelHandler, "response", receive)
    with model_service() as (url, requests):
        definition = project(tmp_path, url)
        system_path = tmp_path / "config/system.yaml"
        system = yaml.safe_load(system_path.read_text())
        system["runtime"] = {"trace": {"mode": "off"}}
        system_path.write_text(yaml.safe_dump(system))
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)
    assert result.output == "Pi answer"
    assert len(requests) == len(observed) == 1
    assert observed[0]["tool_calls"] == []
    assert set(observed[0]) == {"method", "identity", "attempt", "tool_calls"}
    assert result.run.trace_dir is None
