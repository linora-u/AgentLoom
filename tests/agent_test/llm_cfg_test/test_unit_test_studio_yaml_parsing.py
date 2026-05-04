from pathlib import Path

import pytest

from src.lib.logging import initialize_global_logger_once, get_global_logger, set_global_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory


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
