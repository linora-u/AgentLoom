from pathlib import Path

import yaml

from src.lib.smolagents.agent.yaml_agent_factory import YamlConfiguredAgent

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
FIXTURE_WORKER_ROOT = FIXTURE_ROOT / "worker"


def test_all_worker_yamls_use_agent_function_schema_only():
    worker_files = sorted(FIXTURE_WORKER_ROOT.glob("test_*.yaml"))
    assert worker_files, "No worker YAML files found"

    for file_path in worker_files:
        content = file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content) or {}

        assert "agent_function_description" not in data, f"Legacy field still exists: {file_path}"
        assert "agent_function_schema" in data, f"agent_function_schema missing: {file_path}"


def test_all_worker_yamls_have_valid_agent_function_schema():
    worker_files = sorted(FIXTURE_WORKER_ROOT.glob("test_*.yaml"))

    for file_path in worker_files:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        raw_inputs = ((data.get("agent_function_schema") or {}).get("inputs") or {})
        for param_name, param_spec in raw_inputs.items():
            if isinstance(param_spec, dict):
                assert "type" not in param_spec, f"input type should not be configured: {file_path}::{param_name}"

        worker = object.__new__(YamlConfiguredAgent)
        worker._config = data
        worker._normalized = None

        # Should not raise
        worker._normalized = worker._validate_config()

        schema = worker._normalized.agent_function_schema
        assert isinstance(schema, dict), f"Schema not normalized: {file_path}"
        assert isinstance(schema.get("inputs"), dict) and schema["inputs"], f"Inputs missing: {file_path}"
        assert isinstance(schema.get("output"), dict), f"Output missing: {file_path}"
        assert isinstance(schema["output"].get("description"), str) and schema["output"]["description"].strip(), (
            f"Output description missing: {file_path}"
        )
        for param_name, param_spec in schema["inputs"].items():
            assert param_spec.get("type") == "string", f"normalized type must be string: {file_path}::{param_name}"
