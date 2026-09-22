from pathlib import Path

import yaml
from agentloom.application.factory import YamlConfiguredAgent

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
FIXTURE_WORKER_ROOT = FIXTURE_ROOT / "worker"


def test_all_worker_yamls_use_input_schema_only():
    worker_files = sorted(FIXTURE_WORKER_ROOT.glob("test_*.yaml"))
    assert worker_files, "No worker YAML files found"

    for file_path in worker_files:
        content = file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content) or {}

        assert "agent_function_description" not in data, f"Legacy field still exists: {file_path}"
        assert "agent_function_schema" not in data, f"Removed field still exists: {file_path}"
        assert "input_schema" in data, f"input_schema missing: {file_path}"


def test_all_worker_yamls_have_valid_input_schema():
    worker_files = sorted(FIXTURE_WORKER_ROOT.glob("test_*.yaml"))

    for file_path in worker_files:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}

        worker = object.__new__(YamlConfiguredAgent)
        worker._config = data
        worker._normalized = None

        # Should not raise
        worker._normalized = worker._validate_config()

        schema = worker._normalized.input_schema
        assert isinstance(schema, dict), f"Schema not normalized: {file_path}"
        assert schema.get("type") == "object", f"Object root missing: {file_path}"
        assert isinstance(schema.get("properties"), dict) and schema["properties"], (
            f"Properties missing: {file_path}"
        )
        for param_name, param_spec in schema["properties"].items():
            assert isinstance(param_spec.get("type"), str), (
                f"Input type missing: {file_path}::{param_name}"
            )
