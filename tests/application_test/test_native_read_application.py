"""Actual Application/Hook/host/file IO, deterministic model only for CI."""

import json

import pytest
import yaml
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from agentloom.execution.model_protocol import FunctionCallItem, MessageItem, ModelTurnResult

from tests.application_test.native_read_support import (
    SCENARIOS,
    external_read_runtime,
    verify_native,
    write_application,
)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_application_native_read_runs_production_hooks_and_durable_host(tmp_path, scenario):
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.yaml").write_text(
        yaml.safe_dump(
            {
                "runtime": {"root_dir": str(tmp_path / "runtime")},
                "checkpoint": {"enabled": False},
                "self_learning": {"enabled": False},
                "lsp_servers": {"enabled": False},
                "default_toolsets": [],
            }
        )
    )
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: test\n  test: {model: fixture, adapter: openai_chat}\n  summary: {model: fixture, adapter: openai_chat}\n"
    )
    workflow, marker = write_application(tmp_path, "native_case", "test", scenario)
    path = (
        workflow.parent.parent
        / "files"
        / ("denied.txt" if scenario == "excluded" else "missing.txt" if scenario == "missing" else "source.txt")
    )

    class Model:
        def turn(self, *, items, **kwargs):
            if not items:
                return ModelTurnResult(
                    (FunctionCallItem("fixture-read", "native_read", json.dumps({"path": str(path)})),)
                )
            return ModelTurnResult((MessageItem("assistant", "done"),))

    observations = {}
    with external_read_runtime(lambda _: Model(), observations), bind_config(load_project_config(tmp_path)):
        result = execute_app(workflow, file_logging=False)
    verify_native(scenario, observations[result.run.run_id], marker)
