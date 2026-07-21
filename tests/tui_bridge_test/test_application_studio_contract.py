from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

from src.tui_bridge.bridge import TuiBridge
from src.tui_bridge.application_studio import application_detail


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_application_detail_exposes_effective_capabilities_with_sources(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "config/system.yaml",
        """
tools:
  search:
    timeout: 10
tool_access_control:
  mode: allowlist
skills:
  - path: skills/global-review
hooks:
  SessionStart: []
""",
    )
    _write(
        tmp_path / "config/llm.yaml",
        """
model:
  default_model_type: powerful
  powerful:
    model: openai/test
    api_key: must-never-cross-the-bridge
    base_url: https://private.invalid
""",
    )
    _write(
        tmp_path / "skills/global-review/SKILL.md",
        "---\nname: global-review\ndescription: Shared review\n---\n",
    )
    _write(
        tmp_path / "applications/reports/config/system.yaml",
        """
tool_access_control:
  mode: denylist
mcp_servers:
  - reports-db
""",
    )
    _write(
        tmp_path / "applications/reports/skills/local-writer/SKILL.md",
        "---\nname: local-writer\ndescription: Writes reports\n---\n",
    )
    _write(
        tmp_path / "applications/reports/workflows/reports.yaml",
        """
name: report-supervisor
description: Coordinates report creation
model_type: powerful
workflow: Delegate research, then assemble a report.
tools:
  - name: web_search
skills:
  load-mode: eager
  items:
    - path: applications/reports/skills/local-writer
worker_agents:
  - path: researcher.yaml
""",
    )
    _write(
        tmp_path / "applications/reports/workflows/worker_agents/researcher.yaml",
        """
name: researcher
description: Finds evidence
workflow: Find evidence for the requested report.
agent_function_schema:
  description: Research one topic.
  inputs:
    task:
      description: Topic to research.
  output:
    description: Evidence summary.
""",
    )

    detail = TuiBridge(tmp_path).dispatch(
        "application.detail",
        {"application_id": "reports"},
    )

    assert detail["application"]["id"] == "reports"
    assert detail["working_revision"].startswith("sha256:")
    supervisor = detail["agents"][0]
    assert supervisor["role"] == "supervisor"
    assert supervisor["model"] == {"type": "powerful", "source": "agent"}
    assert supervisor["tools"] == [{"name": "web_search", "source": "agent"}]
    assert [(skill["name"], skill["source"], skill["load_mode"]) for skill in supervisor["skills"]] == [
        ("global-review", "global", "on-demand"),
        ("local-writer", "agent", "eager"),
    ]
    assert supervisor["permissions"]["source"] == "application"
    assert supervisor["hooks"]["source"] == "global"
    assert supervisor["mcp"]["source"] == "application"
    assert [worker["name"] for worker in supervisor["workers"]] == ["researcher"]
    assert "must-never-cross-the-bridge" not in str(detail)
    assert "private.invalid" not in str(detail)


def test_application_detail_pins_running_revision_to_the_started_run(tmp_path: Path) -> None:
    workflow = tmp_path / "applications/demo/workflows/demo.yaml"
    _write(workflow, "name: demo\ndescription: Demo\nworkflow: answer\nworker_agents: []\n")
    systems = [{
        "path": "applications/demo/workflows/demo.yaml",
        "application_id": "demo",
        "validation": {"valid": True, "errors": []},
        "latest_run": {"run_id": "run_active", "status": "running"},
    }]
    first = application_detail(tmp_path, "demo", systems=systems)
    _write(
        tmp_path / ".agentloom/runs/demo/run_active/manifest.json",
        json.dumps({
            "application_id": "demo",
            "run_id": "run_active",
            "status": "running",
            "application_revision": first["working_revision"],
        }),
    )
    _write(workflow, "name: demo\ndescription: Changed\nworkflow: answer\nworker_agents: []\n")

    changed = application_detail(tmp_path, "demo", systems=systems)

    assert changed["working_revision"] != first["working_revision"]
    assert changed["running_revision"] == first["working_revision"]


def test_versioned_domain_cli_returns_json_envelopes_and_safe_errors(tmp_path: Path) -> None:
    _write(
        tmp_path / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test:\n    model: openai/test\n    api_key: secret-value\n",
    )
    _write(
        tmp_path / "applications/demo/workflows/demo.yaml",
        "name: demo\ndescription: Demo\nworkflow: answer\nworker_agents: []\n",
    )
    command = [
        sys.executable,
        "-m",
        "src.tui_bridge.domain_cli",
        "--project",
        str(tmp_path),
    ]

    success = subprocess.run(
        [*command, "application.detail", '{"application_id":"demo"}'],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    failure = subprocess.run(
        [*command, "application.detail", '{"application_id":"missing"}'],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert success.returncode == 0
    success_payload = json.loads(success.stdout)
    assert success_payload["contract_version"] == 1
    assert success_payload["ok"] is True
    assert success_payload["result"]["application"]["id"] == "demo"
    assert "secret-value" not in success.stdout
    assert failure.returncode == 2
    assert json.loads(failure.stdout) == {
        "contract_version": 1,
        "ok": False,
        "error": {"code": "not_found", "message": "application not found: missing"},
    }


def test_domain_application_detail_is_paginated_and_bounded_for_large_apps(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test:\n    model: openai/test\n",
    )
    skill_paths: list[str] = []
    for index in range(8):
        skill_path = f"skills/shared-{index}"
        skill_paths.append(skill_path)
        _write(
            tmp_path / skill_path / "SKILL.md",
            f"---\nname: shared-{index}\ndescription: {'shared capability ' * 12}\n---\n",
        )
    _write(
        tmp_path / "config/system.yaml",
        "skills:\n" + "".join(f"  - path: {path}\n" for path in skill_paths),
    )
    for index in range(32):
        _write(
            tmp_path / f"applications/large/workflows/agent-{index:02d}.yaml",
            "\n".join([
                f"name: agent-{index:02d}",
                f"description: {'Large Application capability description. ' * 10}",
                "model_type: test",
                f"workflow: {'Inspect, reason, validate, and report. ' * 20}",
                "tools:",
                "  - name: read_file",
                "  - name: grep_search",
                "worker_agents: []",
                "",
            ]),
        )
    command = [
        sys.executable,
        "-m",
        "src.tui_bridge.domain_cli",
        "--project",
        str(tmp_path),
        "application.detail",
        '{"application_id":"large"}',
    ]

    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert len(completed.stdout.encode("utf-8")) < 24 * 1024
    result = json.loads(completed.stdout)["result"]
    assert result["overview"]["supervisor_count"] == 32
    assert result["page"] == {
        "offset": 0,
        "limit": 10,
        "returned": 10,
        "total": 32,
        "next_offset": 10,
    }
    assert len(result["effective_capabilities"]["skills"]) == 8
    assert all("skill_names" in agent and "skills" not in agent for agent in result["agents"])


def test_domain_impact_distinguishes_one_application_from_global_changes(tmp_path: Path) -> None:
    for application_id in ("alpha", "beta"):
        _write(
            tmp_path / f"applications/{application_id}/workflows/{application_id}.yaml",
            f"name: {application_id}\ndescription: Demo\nworkflow: answer\nworker_agents: []\n",
        )
    command = [
        sys.executable,
        "-m",
        "src.tui_bridge.domain_cli",
        "--project",
        str(tmp_path),
        "application.impact",
    ]

    local = subprocess.run(
        [*command, '{"paths":["applications/alpha/workflows/alpha.yaml"]}'],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    global_change = subprocess.run(
        [*command, '{"paths":["skills/shared/SKILL.md"]}'],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert json.loads(local.stdout)["result"] == {
        "paths": ["applications/alpha/workflows/alpha.yaml"],
        "scope": "application",
        "affected_applications": ["alpha"],
        "count": 1,
    }
    assert json.loads(global_change.stdout)["result"]["affected_applications"] == ["alpha", "beta"]
    assert json.loads(global_change.stdout)["result"]["scope"] == "global"
