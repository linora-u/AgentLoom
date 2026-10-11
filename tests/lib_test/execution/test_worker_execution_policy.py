"""Public execution-policy type and source edges, complementary to Application map tests."""

import pytest
from agentloom.execution.concurrency import resolve_worker_execution


def test_call_override_retains_ordered_sources_without_changing_defaults():
    application = {
        "defaults": {"concurrency": 20},
        "workers": {"workflows/worker_agents/a.yaml": {"concurrency": 8}},
    }
    overridden = resolve_worker_execution(
        "workflows/worker_agents/a.yaml",
        project={"defaults": {"concurrency": 2}},
        application=application,
        concurrency=4,
    )
    assert (overridden.concurrency, overridden.source) == (4, "call_override")
    assert [(entry.source, entry.concurrency) for entry in overridden.source_chain] == [
        ("framework_default", 1),
        ("project_system", 2),
        ("application_system", 20),
        ("application_worker", 8),
        ("call_override", 4),
    ]
    assert (
        resolve_worker_execution(
            "workflows/worker_agents/a.yaml", application=application, concurrency=None
        ).concurrency
        == 8
    )
    assert application["defaults"]["concurrency"] == 20


@pytest.mark.parametrize("invalid", [True, False, "2", "auto", 0, -1, 2.0, None])
@pytest.mark.parametrize("layer", ["project", "application", "unused_worker"])
def test_invalid_lower_source_is_rejected_even_with_valid_call_override(invalid, layer):
    project = {"defaults": {"concurrency": invalid}} if layer == "project" else None
    application = (
        {"defaults": {"concurrency": invalid}}
        if layer == "application"
        else {"workers": {"unused.yaml": {"concurrency": invalid}}}
        if layer == "unused_worker"
        else None
    )
    with pytest.raises(ValueError):
        resolve_worker_execution("a.yaml", project=project, application=application, concurrency=4)


@pytest.mark.parametrize(
    "section",
    [
        {"default": {"concurrency": 2}},
        {"defaults": {"concurency": 2}},
        {"workers": {"a.yaml": {"pending": 20}}},
        {"supervisors": {}},
    ],
)
def test_unknown_execution_fields_are_rejected(section):
    with pytest.raises(ValueError):
        resolve_worker_execution("a.yaml", application=section)


def test_explicit_project_worker_table_is_rejected_even_when_empty():
    with pytest.raises(ValueError, match="Application-private"):
        resolve_worker_execution("a.yaml", project={"workers": {}})


def test_project_loader_rejects_worker_mappings(tmp_path):
    from agentloom.config.system_loader import load_project_system_config

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "system.yaml").write_text("worker_execution:\n  workers: {}\n")
    with pytest.raises(ValueError, match="Application-private"):
        load_project_system_config(tmp_path)


def test_policy_receipt_rejects_unknown_source():
    from agentloom.execution.concurrency import WorkerExecutionSource

    with pytest.raises(ValueError, match="source"):
        WorkerExecutionSource("rpm_inference", 20)
