from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from agentloom.application.composition import build_schedule_mutations
from agentloom.schedules.mutations import ScheduleMutationService
from agentloom.schedules.schema import APPLICATION_SUPERVISOR_VALIDATION
from agentloom.schedules.store import ScheduleStore


def _supervisor(
    project_root: Path,
    relative: str = "applications/demo/workflows/root.yaml",
) -> Path:
    config = project_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "llm.yaml").write_text(
        (
            "model:\n"
            "  default_model_type: test\n"
            "  test: {model: openai/test, adapter: openai_chat}\n"
            "  summary: {model: openai/test-summary, adapter: openai_chat}\n"
        ),
        encoding="utf-8",
    )
    path = project_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "name: supervisor\n"
            "agent_runtime: smolagents\n"
            "description: Scheduled supervisor.\n"
            "workflow: Run the scheduled task.\n"
        ),
        encoding="utf-8",
    )
    return path


def _markdown_supervisor(
    project_root: Path,
    relative: str = "applications/markdown/workflows/root.md",
) -> Path:
    _supervisor(project_root)
    path = project_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "```yaml\n"
        + yaml.safe_dump(
            {
                "name": "markdown-supervisor",
                "agent_runtime": "smolagents",
                "description": "Scheduled Markdown supervisor.",
            },
            sort_keys=False,
        )
        + "```\n\nRun the scheduled Markdown task.\n",
        encoding="utf-8",
    )
    return path


def test_schedule_add_requires_application_resolver_before_storage_creation(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        RuntimeError,
        match="requires an Application Supervisor resolver",
    ):
        ScheduleMutationService(tmp_path).add(
            yaml_path="applications/demo/workflows/root.yaml",
            name="job",
            schedule={"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert not (tmp_path / ".agentloom").exists()


def test_schedule_add_rejects_invalid_name_before_resolving_or_creating_storage(
    tmp_path: Path,
) -> None:
    resolver_calls: list[str | Path] = []

    def resolver(path: str | Path) -> Path:
        resolver_calls.append(path)
        raise AssertionError("invalid name reached the Application resolver")

    with pytest.raises(ValueError, match="control characters"):
        ScheduleMutationService(
            tmp_path,
            target_resolver=resolver,
        ).add(
            yaml_path="applications/demo/workflows/root.yaml",
            name="bad\nname",
            schedule={"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert resolver_calls == []
    assert not (tmp_path / ".agentloom").exists()


def test_schedule_add_rejects_untyped_resolver_result_before_storage(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        TypeError,
        match="must return ValidatedScheduleTarget",
    ):
        ScheduleMutationService(
            tmp_path,
            target_resolver=lambda _path: tmp_path / "root.yaml",
        ).add(
            yaml_path="applications/demo/workflows/root.yaml",
            name="job",
            schedule={"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert not (tmp_path / ".agentloom").exists()


def test_application_composition_adds_only_valid_project_supervisor(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(tmp_path)

    job = build_schedule_mutations(tmp_path).add(
        yaml_path=supervisor.relative_to(tmp_path).as_posix(),
        name="scheduled",
        schedule={"kind": "interval", "every": "1h", "timezone": "UTC"},
    )

    assert ScheduleStore(tmp_path).get_job(job["id"])["yaml_path"] == (supervisor.relative_to(tmp_path).as_posix())
    assert job["target_validation"] == APPLICATION_SUPERVISOR_VALIDATION


def test_application_composition_accepts_markdown_supervisor(
    tmp_path: Path,
) -> None:
    supervisor = _markdown_supervisor(tmp_path)

    job = build_schedule_mutations(tmp_path).add(
        yaml_path=supervisor.relative_to(tmp_path).as_posix(),
        name="markdown",
        schedule={
            "kind": "once",
            "at": "2099-01-02T03:04:05Z",
            "timezone": "UTC",
        },
    )

    assert ScheduleStore(tmp_path).get_job(job["id"])["yaml_path"].endswith("root.md")


def test_application_named_worker_agents_can_own_a_supervisor(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(
        tmp_path,
        "applications/worker_agents/demo/workflows/root.yaml",
    )

    job = build_schedule_mutations(tmp_path).add(
        yaml_path=supervisor.relative_to(tmp_path).as_posix(),
        name="nested application",
        schedule={"kind": "interval", "every": "1h", "timezone": "UTC"},
    )

    assert ScheduleStore(tmp_path).get_job(job["id"])["yaml_path"] == (supervisor.relative_to(tmp_path).as_posix())


@pytest.mark.parametrize(
    "target",
    ["worker", "absolute", "parent_traversal", "symlink", "invalid"],
)
def test_application_composition_rejects_invalid_targets_before_storage(
    tmp_path: Path,
    target: str,
) -> None:
    supervisor = _supervisor(tmp_path)
    if target == "worker":
        candidate_path = supervisor.parent / "worker_agents/worker.yaml"
        candidate_path.parent.mkdir()
        candidate_path.write_text(
            (
                "name: worker\n"
                "agent_runtime: smolagents\n"
                "description: Worker.\n"
                "agent_function_schema:\n"
                "  description: Work.\n"
                "  inputs: {task: {description: Task, required: true}}\n"
                "  output: {description: Result}\n"
            ),
            encoding="utf-8",
        )
        candidate = candidate_path.relative_to(tmp_path).as_posix()
    elif target == "absolute":
        candidate = str(supervisor)
    elif target == "parent_traversal":
        candidate = "applications/demo/workflows/../workflows/root.yaml"
    elif target == "symlink":
        outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
        outside.write_text(supervisor.read_text(encoding="utf-8"), encoding="utf-8")
        supervisor.unlink()
        supervisor.symlink_to(outside)
        candidate = supervisor.relative_to(tmp_path).as_posix()
    else:
        supervisor.write_text("name: invalid\n", encoding="utf-8")
        candidate = supervisor.relative_to(tmp_path).as_posix()

    with pytest.raises(ValueError):
        build_schedule_mutations(tmp_path).add(
            yaml_path=candidate,
            name="invalid",
            schedule={"kind": "interval", "every": "1h", "timezone": "UTC"},
        )

    assert not (tmp_path / ".agentloom").exists()
