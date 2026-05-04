from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR_YAML = PROJECT_ROOT / "applications/ai_check/workflows/ai_check_agent.yaml"
WORKER_ROOT = PROJECT_ROOT / "applications/ai_check/workflows/worker_agents"
EXPECTED_WORKER_FILES = {
    "step0_preparation.yaml",
    "step1_shared_variables_identification.yaml",
    "step2_access_interface_analysis.yaml",
    "step3_risk_identification.yaml",
    "step4_scenario_coverage.yaml",
    "step5_recommendations.yaml",
    "step6_report_integration.yaml",
}


def _load_yaml(file_path: Path) -> dict:
    return yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}


def _load_supervisor_path_validation() -> list[dict]:
    supervisor_data = _load_yaml(SUPERVISOR_YAML)
    return ((supervisor_data.get("tool_access_control") or {}).get("path_validation") or [])


def test_ai_check_worker_directory_contains_expected_workers():
    worker_files = {file_path.name for file_path in WORKER_ROOT.glob("*.yaml")}
    assert worker_files == EXPECTED_WORKER_FILES


def test_ai_check_workers_define_tool_access_control():
    for worker_name in sorted(EXPECTED_WORKER_FILES):
        worker_data = _load_yaml(WORKER_ROOT / worker_name)
        tool_access_control = worker_data.get("tool_access_control") or {}
        assert tool_access_control, f"tool_access_control missing: {worker_name}"
        assert tool_access_control.get("path_validation"), f"path_validation missing: {worker_name}"


def test_ai_check_worker_path_validation_matches_supervisor():
    expected_path_validation = _load_supervisor_path_validation()
    assert expected_path_validation, "Supervisor path_validation must stay configured"

    for worker_name in sorted(EXPECTED_WORKER_FILES):
        worker_data = _load_yaml(WORKER_ROOT / worker_name)
        worker_path_validation = ((worker_data.get("tool_access_control") or {}).get("path_validation") or [])
        assert worker_path_validation == expected_path_validation, (
            f"Worker path_validation drifted from supervisor: {worker_name}"
        )
