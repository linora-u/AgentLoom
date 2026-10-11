"""Controlled Runtime fixture shared across independent Application test suites."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Condition

import pytest
import yaml
from agentloom.execution.agent_runtime import AgentRuntimeResult, RuntimeCapabilities, RuntimeUsage


class Probe:
    def __init__(self):
        self.condition = Condition()
        self.starts = []
        self.closed = []
        self.active = 0
        self.peak = 0
        self.behavior = lambda payload: payload
        self.close_behavior = lambda payload: None
        self.construct_error = None
        self.root_behavior = None
        self.resource_close = None

    def wait_starts(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.starts) >= count, timeout=10), self.starts


@pytest.fixture
def batch_project(tmp_path, monkeypatch):
    from agentloom.app.composition import build_builtin_runtime_registry
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.execution.trace import capture_explicit_execution_context

    def write(path, config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(config))

    write(
        tmp_path / "config/system.yaml",
        {
            "runtime": {"root_dir": str(tmp_path / "runtime")},
            "checkpoint": {"enabled": False},
            "self_learning": {"enabled": False},
            "logging": {"console_enabled": False},
            "default_toolsets": [],
        },
    )
    write(
        tmp_path / "config/model.yaml",
        {
            "model": {
                "default_model_type": "test",
                "test": {"model": "fixture/model", "adapter": "openai_chat", "api_key": "fixture-secret"},
            }
        },
    )
    application = tmp_path / "applications/batch"
    worker = application / "workflows/worker.yaml"
    write(
        worker,
        {
            "name": "worker",
            "description": "Controlled Worker.",
            "task": "Process supplied input.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
            "input_schema": {
                "type": "object",
                "properties": {"value": {}, "action": {}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    )
    probe = Probe()

    class Runtime:
        runtime_id = "batch-fixture"
        capabilities = RuntimeCapabilities(False, False, True, True)

        def __init__(self, definition):
            self.definition = definition
            self.payload = None
            self.closed = False
            if probe.construct_error is not None:
                raise probe.construct_error

        def run(self, request):
            if "Input data (JSON):\n" not in request.task:
                return AgentRuntimeResult(state="success", output=probe.root_behavior())
            self.payload = json.loads(request.task.split("Input data (JSON):\n", 1)[1])
            execution = capture_explicit_execution_context()
            if probe.resource_close is not None:
                from agentloom.execution.resources import register_resource

                register_resource(
                    "same-resource-key",
                    lambda: probe.resource_close(self.payload, self.definition.instance_id),
                    instance_id=self.definition.instance_id,
                )
            with probe.condition:
                probe.starts.append((self.payload, execution, self.definition, request))
                probe.active += 1
                probe.peak = max(probe.peak, probe.active)
                probe.condition.notify_all()
            return AgentRuntimeResult(
                state="success",
                output=probe.behavior(self.payload),
                usage=RuntimeUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            )

        def snapshot(self):
            return None

        def close(self):
            if self.closed:
                return
            probe.close_behavior(self.payload)
            self.definition.tool_gateway.close()
            self.closed = True
            if self.payload is not None:
                with probe.condition:
                    probe.closed.append(self.payload)
                    probe.active -= 1
                    probe.condition.notify_all()

    registry = build_builtin_runtime_registry()
    registry.register("batch-fixture", capabilities=Runtime.capabilities, factory=Runtime)
    monkeypatch.setattr("agentloom.app.validation.build_builtin_runtime_registry", lambda: registry)
    monkeypatch.setattr("agentloom.app.agent.build_builtin_runtime_registry", lambda: registry)
    original_cwd = Path.cwd()
    try:
        with bind_config(load_project_config(tmp_path)):
            yield tmp_path, application, worker, probe, write
    finally:
        os.chdir(original_cwd)
