"""Public contract for the framework skill's read-only Application scanner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[4] / "agentloom-framework-skill/scripts/scan_tools.py"
SPEC = importlib.util.spec_from_file_location("agentloom_scan_tools", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
scan_app_structure = MODULE.scan_app_structure
extract_agent_prompts = MODULE.extract_agent_prompts


@pytest.fixture
def application(tmp_path: Path) -> tuple[Path, Path]:
    workflows = tmp_path / "applications/demo/workflows"
    workers = workflows / "worker_agents"
    workers.mkdir(parents=True)
    supervisor = workflows / "supervisor.yaml"
    supervisor.write_text(
        "name: supervisor\n"
        "agent_runtime: smolagents\n"
        "description: Coordinate the worker.\n"
        "system_prompt: Keep the evidence.\n"
        "task:\n  - Inspect the note.\n  - Summarize the findings.\n"
        "worker_agents:\n  - path: worker_agents/worker.yaml\n",
        encoding="utf-8",
    )
    worker = workers / "worker.yaml"
    worker.write_text(
        "name: worker\n"
        "agent_runtime: smolagents\n"
        "description: Read the note.\n"
        "task: Read the supplied file.\n"
        "input_schema:\n"
        "  type: object\n"
        "  properties: {file_path: {type: string}}\n"
        "  required: [file_path]\n"
        "  additionalProperties: false\n",
        encoding="utf-8",
    )
    return supervisor, worker


def test_scan_reports_yaml_supervisor_worker_and_schema(application):
    supervisor, _ = application
    summary = scan_app_structure(str(supervisor.parents[1]))
    assert "supervisor.yaml (Supervisor)" in summary
    assert "worker.yaml (Worker)" in summary
    assert "file_path" in summary
    assert "worker_agents/worker.yaml" in summary


def test_extract_prompts_preserves_task_order_and_system_role(application):
    supervisor, _ = application
    result = extract_agent_prompts(str(supervisor))
    assert "## system_prompt\n\nKeep the evidence." in result
    assert result.index("## task 1\n\nInspect the note.") < result.index(
        "## task 2\n\nSummarize the findings."
    )


def test_extract_prompts_preserves_worker_task(application):
    _, worker = application
    assert "## task 1\n\nRead the supplied file." in extract_agent_prompts(str(worker))


def test_extract_prompts_shows_file_backed_system_prompt_path(application):
    supervisor, _ = application
    supervisor.write_text(
        supervisor.read_text().replace(
            "system_prompt: Keep the evidence.",
            "system_prompt:\n  path: prompts/reviewer.md",
        )
    )
    result = extract_agent_prompts(str(supervisor))
    assert "path: prompts/reviewer.md" in result


def test_markdown_definition_is_rejected(application):
    supervisor, _ = application
    legacy = supervisor.with_suffix(".md")
    legacy.write_text("```yaml\nname: legacy\n```\nold task\n")
    assert "必须是 YAML" in extract_agent_prompts(str(legacy))
    assert "legacy.md" not in scan_app_structure(str(supervisor.parents[1]))


def test_bad_yaml_reports_parse_error_without_exposing_content(tmp_path: Path):
    source = tmp_path / "broken.yaml"
    source.write_text("name: one\nname: secret-sentinel\n", encoding="utf-8")
    result = extract_agent_prompts(str(source))
    assert "解析失败" in result
    assert "secret-sentinel" not in result


def test_missing_application_is_an_actionable_error(tmp_path: Path):
    result = scan_app_structure(str(tmp_path / "missing"))
    assert "路径不存在" in result
    assert "当前工作目录" in result


def test_scan_is_read_only(application):
    supervisor, worker = application
    app = supervisor.parents[1]
    before = {p.relative_to(app): p.read_bytes() for p in app.rglob("*") if p.is_file()}
    scan_app_structure(str(app))
    extract_agent_prompts(str(worker))
    after = {p.relative_to(app): p.read_bytes() for p in app.rglob("*") if p.is_file()}
    assert after == before
