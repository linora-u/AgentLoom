from pathlib import Path


def test_archive_legacy_agent_workspaces_moves_tree_under_runtime_home(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.workspace_migration import archive_legacy_agent_workspaces

    legacy_root = tmp_path / ".runtime"
    (legacy_root / "supervisor" / "worker").mkdir(parents=True)
    (legacy_root / "supervisor" / "insights.md").write_text(
        "# Insights\n", encoding="utf-8"
    )
    (legacy_root / "supervisor" / "worker" / "trace.md").write_text(
        "# Trace\n", encoding="utf-8"
    )
    runtime_root = tmp_path / ".agentloom"

    result = archive_legacy_agent_workspaces(legacy_root, runtime_root)

    assert result.archive_dir is not None
    assert result.file_count == 2
    assert not legacy_root.exists()
    assert result.archive_dir.is_relative_to(
        runtime_root / "workspaces" / "legacy-unscoped"
    )
    assert (result.archive_dir / "supervisor" / "insights.md").exists()
    assert (result.archive_dir / "supervisor" / "worker" / "trace.md").exists()


def test_preview_legacy_agent_workspaces_does_not_move_files(tmp_path: Path) -> None:
    from src.lib.runtime.workspace_migration import preview_legacy_agent_workspaces

    legacy_root = tmp_path / ".runtime"
    (legacy_root / "agent").mkdir(parents=True)
    payload = legacy_root / "agent" / "context.md"
    payload.write_text("context", encoding="utf-8")

    result = preview_legacy_agent_workspaces(legacy_root)

    assert result.file_count == 1
    assert result.total_bytes == len("context")
    assert result.archive_dir is None
    assert payload.exists()


def test_archive_legacy_agent_workspaces_rejects_runtime_inside_source(
    tmp_path: Path,
) -> None:
    import pytest

    from src.lib.runtime.workspace_migration import archive_legacy_agent_workspaces

    legacy_root = tmp_path / ".runtime"
    legacy_root.mkdir()

    with pytest.raises(ValueError, match="inside legacy_workspace_dir"):
        archive_legacy_agent_workspaces(
            legacy_root,
            legacy_root / ".agentloom",
        )


def test_archive_legacy_agent_workspaces_rejects_symlinked_workspaces(
    tmp_path: Path,
) -> None:
    import pytest

    from src.lib.runtime.workspace_migration import archive_legacy_agent_workspaces

    legacy_root = tmp_path / ".runtime"
    legacy_root.mkdir()
    runtime_root = tmp_path / ".agentloom"
    runtime_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime_root / "workspaces").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="safe directory"):
        archive_legacy_agent_workspaces(legacy_root, runtime_root)

    assert legacy_root.exists()
    assert list(outside.iterdir()) == []
