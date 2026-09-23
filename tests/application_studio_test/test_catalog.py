from __future__ import annotations

import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from agentloom.app.studio.catalog import project_catalog
from agentloom.app.studio.query_service import StudioQueryService

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _system(path: str, *, application_id: str = "demo") -> dict:
    return {
        "id": path,
        "path": path,
        "application_id": application_id,
        "name": "demo-supervisor",
        "description": "Coordinates demo workers",
        "state": "running",
    }


def test_project_catalog_aggregates_applications_and_agent_configuration(tmp_path: Path) -> None:
    supervisor = "applications/demo/workflows/demo.yaml"
    _write(
        tmp_path / supervisor,
        """
name: demo-supervisor
description: Coordinates demo workers
skills:
  paths:
    - skills
worker_agents:
  - path: worker.yaml
""",
    )
    _write(
        tmp_path / "applications/demo/workflows/worker_agents/worker.yaml",
        """
name: fact-worker
description: Finds facts
skills:
  paths:
    - skills/fact-checking
worker_agents: []
""",
    )

    runs = [
        {
            "run_id": "run-active",
            "application_id": "demo",
            "system_id": supervisor,
            "status": "running",
            "started_at": "2026-07-18T07:00:00+00:00",
        },
        {
            "run_id": "run-complete",
            "application_id": "demo",
            "system_id": supervisor,
            "status": "completed",
            "started_at": "2026-07-17T07:00:00+00:00",
        },
    ]

    catalog = project_catalog(tmp_path, [_system(supervisor)], runs, now=NOW)

    assert catalog["applications"] == [
        {
            "id": "demo",
            "name": "demo",
            "path": "applications/demo",
            "system_count": 1,
            "worker_count": 1,
            "skill_count": 0,
            "run_count": 2,
            "active_run_count": 1,
        }
    ]
    assert catalog["agents"] == [
        {
            "id": supervisor,
            "application_id": "demo",
            "name": "demo-supervisor",
            "description": "Coordinates demo workers",
            "path": supervisor,
            "role": "supervisor",
            "skills": {"paths": ["skills"]},
            "workers": [
                {
                    "id": "applications/demo/workflows/worker_agents/worker.yaml",
                    "application_id": "demo",
                    "name": "fact-worker",
                    "description": "Finds facts",
                    "path": "applications/demo/workflows/worker_agents/worker.yaml",
                    "role": "worker",
                    "skills": {"paths": ["skills/fact-checking"]},
                    "workers": [],
                }
            ],
        }
    ]


def test_bootstrap_parses_each_agent_definition_once_and_keeps_shared_worker_trees(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write(
        tmp_path / "config/llm.yaml",
        "model:\n  summary:\n    model: openai/test-summary\n  default_model_type: test\n  test:\n    model: openai/test\n",
    )
    alpha = "applications/demo/workflows/alpha.yaml"
    beta = "applications/demo/workflows/beta.yaml"
    worker = "applications/demo/workflows/worker_agents/shared.yaml"
    definitions = {
        alpha: """\
name: alpha
description: alpha supervisor
workflow: delegate alpha
worker_agents:
  - path: shared.yaml
""",
        beta: """\
name: beta
description: beta supervisor
workflow: delegate beta
worker_agents:
  - path: shared.yaml
""",
        worker: """\
name: shared
description: shared worker
workflow: do shared work
""",
    }
    for relative, payload in definitions.items():
        _write(tmp_path / relative, payload)

    parse_counts: Counter[str] = Counter()
    from agentloom.app import definition as definition_module
    original_safe_load = definition_module.load_unique_yaml

    def count_definition_parse(stream):
        text = stream.read() if hasattr(stream, "read") else stream
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        if isinstance(text, str):
            for relative, payload in definitions.items():
                if text == payload:
                    parse_counts[relative] += 1
                    break
        return original_safe_load(text)

    monkeypatch.setattr(definition_module, "load_unique_yaml", count_definition_parse)

    result = StudioQueryService(tmp_path).bootstrap()

    assert parse_counts == Counter({alpha: 1, beta: 1, worker: 1})
    assert [
        {
            "id": agent["id"],
            "name": agent["name"],
            "role": agent["role"],
            "workers": [
                {
                    "id": child["id"],
                    "name": child["name"],
                    "role": child["role"],
                }
                for child in agent["workers"]
            ],
        }
        for agent in result["agents"]
    ] == [
        {
            "id": alpha,
            "name": "alpha",
            "role": "supervisor",
            "workers": [{"id": worker, "name": "shared", "role": "worker"}],
        },
        {
            "id": beta,
            "name": "beta",
            "role": "supervisor",
            "workers": [{"id": worker, "name": "shared", "role": "worker"}],
        },
    ]
    assert result["applications"] == [
        {
            "id": "demo",
            "name": "demo",
            "path": "applications/demo",
            "system_count": 2,
            "worker_count": 1,
            "skill_count": 0,
            "run_count": 0,
            "active_run_count": 0,
        }
    ]


def test_application_skills_are_project_local_and_never_follow_symlinks(tmp_path: Path) -> None:
    supervisor = "applications/demo/workflows/demo.yaml"
    _write(tmp_path / supervisor, "name: demo\nworker_agents: []\n")
    _write(
        tmp_path / "applications/demo/skills/reviewer/SKILL.md",
        """---
name: careful-reviewer
description: Reviews a change before delivery
---
# Reviewer
""",
    )
    external = tmp_path / "outside"
    _write(
        external / "stolen/SKILL.md",
        "---\nname: stolen\ndescription: must not be visible\n---\n",
    )
    (tmp_path / "applications/demo/skills/linked-dir").symlink_to(external / "stolen", target_is_directory=True)
    (tmp_path / "applications/demo/skills/linked-file.md").symlink_to(external / "stolen/SKILL.md")
    _write(
        tmp_path / "applications/demo/data/skills/not-installed/SKILL.md",
        "---\nname: fixture-only\ndescription: data, not an installed Application Skill\n---\n",
    )

    catalog = project_catalog(tmp_path, [_system(supervisor)], [], now=NOW)

    assert catalog["skills"] == [
        {
            "id": "demo:careful-reviewer",
            "application_id": "demo",
            "name": "careful-reviewer",
            "description": "Reviews a change before delivery",
            "origin": "application",
            "path": "applications/demo/skills/reviewer/SKILL.md",
        }
    ]
    assert catalog["applications"][0]["skill_count"] == 1


def test_global_skills_are_runtime_global_and_not_application_or_framework_skills(
    tmp_path: Path,
) -> None:
    supervisor = "applications/demo/workflows/demo.yaml"
    _write(tmp_path / supervisor, "name: demo\nworker_agents: []\n")
    _write(
        tmp_path / "skills/global-review/SKILL.md",
        "---\nname: global-review\ndescription: Shared runtime review\n---\n",
    )
    _write(
        tmp_path / "applications/demo/skills/local-only/SKILL.md",
        "---\nname: local-only\ndescription: Application package\n---\n",
    )
    _write(
        tmp_path / "agentloom-framework-skill/SKILL.md",
        "---\nname: agentloom-framework-skill\ndescription: Studio control plane\n---\n",
    )

    catalog = project_catalog(tmp_path, [_system(supervisor)], [], now=NOW)

    assert [
        (skill["name"], skill["origin"], skill["application_id"])
        for skill in catalog["skills"]
    ] == [
        ("global-review", "global", None),
        ("local-only", "application", "demo"),
    ]


def test_unsafe_worker_references_and_symlinked_worker_files_are_omitted(tmp_path: Path) -> None:
    supervisor = "applications/demo/workflows/demo.yaml"
    _write(
        tmp_path / supervisor,
        """
name: demo
worker_agents:
  - path: ../../../../outside.yaml
  - path: linked.yaml
""",
    )
    _write(tmp_path / "outside.yaml", "name: outside\n")
    worker_dir = tmp_path / "applications/demo/workflows/worker_agents"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "linked.yaml").symlink_to(tmp_path / "outside.yaml")

    catalog = project_catalog(tmp_path, [_system(supervisor)], [], now=NOW)

    assert catalog["agents"][0]["workers"] == []
    assert catalog["applications"][0]["worker_count"] == 0


def test_empty_catalog_is_read_only_and_does_not_create_schedule_storage(tmp_path: Path) -> None:
    catalog = project_catalog(tmp_path, [], [], now=NOW)

    assert catalog == {
        "applications": [],
        "agents": [],
        "skills": [],
        "schedules": {
            "items": [],
            "service": {
                "state": "stopped",
                "pid": None,
                "started_at": None,
                "last_tick_at": None,
                "last_success_at": None,
                "last_error": None,
                "job_count": 0,
                "due_count": 0,
                "claimed_count": 0,
                "execution_count": 0,
            },
        },
    }
    assert not (tmp_path / ".agentloom").exists()


def test_catalog_does_not_reexport_schedule_projection() -> None:
    from agentloom.app.studio import catalog

    assert not hasattr(catalog, "schedule_catalog")


def test_catalog_import_does_not_load_agent_or_model_runtime() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from agentloom.app.studio.catalog import project_catalog; "
                "assert project_catalog; "
                "assert 'agentloom.app.agent' not in sys.modules; "
                "assert 'agentloom.app.runner' not in sys.modules; "
                "assert 'agentloom.schedules.runner' not in sys.modules; "
                "assert 'agentloom.schedules.service' not in sys.modules; "
                "assert 'agentloom.schedules.store' not in sys.modules; "
                "assert 'litellm' not in sys.modules"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
