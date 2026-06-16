from pathlib import Path

import pytest

from src.lib.logging import initialize_global_logger_once, get_global_logger, set_global_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
from src.lib.smolagents.agent.yaml_agent_factory import YamlConfiguredSupervisorAgent


@pytest.fixture(autouse=True)
def _ensure_global_logger():
    prev = get_global_logger(create_if_missing=False)
    if prev is None:
        initialize_global_logger_once("test_unit_test_studio_yaml")
    yield
    if prev is None:
        set_global_logger(prev)


def test_unit_test_studio_supervisor_and_workers_parse():
    repo_root = Path(__file__).resolve().parents[3]
    app_root = repo_root / "applications" / "unit_test_studio"
    supervisor_yaml = app_root / "workflows" / "unit_test_studio_agent.yaml"

    assert supervisor_yaml.exists(), f"missing supervisor yaml: {supervisor_yaml}"

    supervisor_cfg = YamlAgentFactory._load_config_from_file(supervisor_yaml)
    workers = supervisor_cfg.get("worker_agents", [])
    assert len(workers) == 5

    for worker in workers:
        worker_path = repo_root / worker["path"]
        assert worker_path.exists(), f"missing worker yaml: {worker_path}"
        worker_cfg = YamlAgentFactory._load_config_from_file(worker_path)
        assert isinstance(worker_cfg, dict)
        assert worker_cfg.get("name")
        assert worker_cfg.get("workflow")


def test_unit_test_studio_code_act_contracts_are_strict():
    repo_root = Path(__file__).resolve().parents[3]
    app_root = repo_root / "applications" / "unit_test_studio"
    supervisor_yaml = app_root / "workflows" / "unit_test_studio_agent.yaml"
    worker_dir = app_root / "workflows" / "worker_agents"

    config_paths = [supervisor_yaml, *sorted(worker_dir.glob("*.yaml"))]
    for config_path in config_paths:
        cfg = YamlAgentFactory._load_config_from_file(config_path)
        workflow = cfg["workflow"]
        assert "CodeAct Execution Contract" in workflow
        assert "Every action MUST be emitted as a Python `<code>...</code>` block" in workflow
        assert "Final output MUST be produced by `final_answer(...)` inside a `<code>` block" in workflow
        assert "Never return bare" in workflow

    assert "planning_interval" not in YamlAgentFactory._load_config_from_file(supervisor_yaml)
    for worker_yaml in worker_dir.glob("*.yaml"):
        cfg = YamlAgentFactory._load_config_from_file(worker_yaml)
        assert "planning_interval" not in cfg


def test_unit_test_studio_supervisor_payload_extraction_survives_task_wrapping():
    repo_root = Path(__file__).resolve().parents[3]
    app_root = repo_root / "applications" / "unit_test_studio"
    supervisor_yaml = app_root / "workflows" / "unit_test_studio_agent.yaml"
    cfg = YamlAgentFactory._load_config_from_file(supervisor_yaml)

    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = cfg
    supervisor._logger = None

    transformed = supervisor._transform_task(
        'Generate tests.\nUse this JSON payload exactly:\n{"target_root":"x","targets":"y","output_dir":"z"}'
    )

    assert "_before_payload, marker_text, payload_text = text.rpartition(marker)" in transformed
    assert "payload_text = payload_text.lstrip()" in transformed
    assert "payload_text = text.lstrip()" not in transformed


def test_unit_test_studio_workers_register_as_tools_when_schema_present():
    repo_root = Path(__file__).resolve().parents[3]
    app_root = repo_root / "applications" / "unit_test_studio"
    worker_dir = app_root / "workflows" / "worker_agents"

    worker_files = sorted(worker_dir.glob("*.yaml"))
    assert len(worker_files) == 5

    tool_count = 0
    for worker_yaml in worker_files:
        cfg = YamlAgentFactory._load_config_from_file(worker_yaml)
        tool = YamlAgentFactory.create_agent_as_tool(cfg, model=object())
        if cfg.get("agent_function_schema") is None:
            assert tool is None
        else:
            assert tool is not None, f"worker should register as tool: {worker_yaml}"
            assert tool.__name__ == cfg["name"]
            tool_count += 1

    assert tool_count >= 1
