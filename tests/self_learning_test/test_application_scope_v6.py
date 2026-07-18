from __future__ import annotations


def test_self_learning_uses_the_runtime_canonical_application_id() -> None:
    from src.extensions.self_learning.application_scope import (
        safe_application_id as self_learning_application_id,
    )
    from src.lib.runtime import safe_application_id as runtime_application_id

    raw = "commerce/中文应用"

    assert self_learning_application_id(raw) == runtime_application_id(raw)


def test_bound_runtime_application_id_wins_over_config_fallback(tmp_path) -> None:
    from src.extensions.self_learning.application_scope import resolve_application_scope
    from src.lib.runtime import RuntimeContext, bind_run_context

    runtime = RuntimeContext(
        root_dir=tmp_path / ".agentloom",
        application_id="bound/app",
        task_id="task_1",
        run_id="run_1",
    )

    with bind_run_context(runtime):
        scope = resolve_application_scope({"application_id": "configured/app"})

    assert scope.application_id == "bound/app"


def test_legacy_application_id_uses_one_authoritative_workflow_path() -> None:
    from src.extensions.self_learning.application_scope import (
        resolve_legacy_application_id,
    )

    resolution = resolve_legacy_application_id(
        "old-agent-name",
        workflow_paths=("/workspace/applications/commerce/search/workflows/main.yaml",),
    )

    assert resolution.canonical_id == "commerce/search"
    assert resolution.quarantine_id == ""
    assert resolution.reason == "workflow_path"


def test_legacy_application_id_quarantines_conflicting_paths() -> None:
    from src.extensions.self_learning.application_scope import (
        resolve_legacy_application_id,
    )

    resolution = resolve_legacy_application_id(
        "old-agent-name",
        workflow_paths=(
            "/workspace/applications/app_a/workflows/main.yaml",
            "/workspace/applications/app_b/workflows/main.yaml",
        ),
    )

    assert resolution.canonical_id == ""
    assert resolution.quarantine_id.startswith("migration-unresolved/")
    assert resolution.reason == "conflicting_application_paths"


def test_already_canonical_legacy_application_id_maps_losslessly() -> None:
    from src.extensions.self_learning.application_scope import (
        resolve_legacy_application_id,
    )

    resolution = resolve_legacy_application_id("commerce/search")

    assert resolution.canonical_id == "commerce/search"
    assert resolution.quarantine_id == ""
    assert resolution.reason == "canonical_identity"
