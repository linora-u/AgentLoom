"""Production Application entry; deterministic model only at the CI boundary."""
import json
from pathlib import Path
import pytest
import yaml
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from agentloom.runtime.model_protocol import FunctionCallItem, MessageItem, ModelTurnResult
from tests.application_test.native_write_shell_support import SCENARIOS, external_write_runtime, planned_calls, verify_native, write_application

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_application_governs_native_mutation_and_shell(tmp_path, scenario):
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.yaml").write_text(yaml.safe_dump({"runtime": {"root_dir": str(tmp_path / "runtime")}, "checkpoint": {"enabled": False}, "self_learning": {"enabled": False}, "lsp_servers": {"enabled": False}, "default_toolsets": []}))
    (config / "llm.yaml").write_text("model:\n  default_model_type: test\n  test: {model: fixture, adapter: openai_chat}\n  summary: {model: fixture, adapter: openai_chat}\n")
    workflow, marker = write_application(tmp_path, "write_case", "test", scenario)
    plan = planned_calls(workflow.parent.parent / "files", scenario, marker)
    class Model:
        def __init__(self):
            self.n = 0
        def turn(self, **kwargs):
            if self.n < len(plan):
                name, arguments = plan[self.n]
                self.n += 1
                return ModelTurnResult((FunctionCallItem(str(self.n), name, json.dumps(arguments)),))
            return ModelTurnResult((MessageItem("assistant", "done"),))
    observations = {}
    with external_write_runtime(lambda _: Model(), observations), bind_config(load_project_config(tmp_path)):
        result = execute_app(workflow, file_logging=False)
    verify_native(scenario, observations[result.run.run_id], marker)
