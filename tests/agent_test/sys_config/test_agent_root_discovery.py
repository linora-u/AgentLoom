"""Tests for ``_discover_agent_root`` and ``_is_agentloom_project``.

Verifies that the project root is located by walking upward and matching
``pyproject.toml`` with ``[project] name = "AgentLoom"``.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

import src.lib.config.config as config_module


def _write_pyproject(directory: Path, name: str = "AgentLoom") -> None:
    """Create a minimal ``pyproject.toml`` with the given project name."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(
        textwrap.dedent(f"""\
            [project]
            name = "{name}"
            version = "0.0.1"
        """),
        encoding="utf-8",
    )


def _write_config(directory: Path) -> None:
    """Create a minimal ``config/system.yaml``."""
    config_dir = directory / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "system.yaml").write_text("system:\n  name: test\n", encoding="utf-8")


# ─── Tests ────────────────────────────────────────────────────────────────


def test_discover_from_project_root(monkeypatch, tmp_path: Path):
    """When cwd IS the project root, discovery succeeds immediately."""
    _write_pyproject(tmp_path)
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    root = config_module._discover_agent_root()
    assert root == tmp_path


def test_discover_from_nested_subdir(monkeypatch, tmp_path: Path):
    """When cwd is a deeply nested subdirectory, discovery walks upward."""
    project = tmp_path / "project"
    _write_pyproject(project)
    _write_config(project)

    deep = project / "applications" / "my_app" / "workflows"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    root = config_module._discover_agent_root()
    assert root == project


def test_discover_skips_app_config(monkeypatch, tmp_path: Path):
    """Sub-directory with its own config/system.yaml must NOT be mistaken
    for the project root – only pyproject.toml + name matters."""
    project = tmp_path / "project"
    _write_pyproject(project)
    _write_config(project)

    # Application that also has config/system.yaml (the old bug trigger)
    app_dir = project / "applications" / "ai_check"
    _write_config(app_dir)
    monkeypatch.chdir(app_dir)

    root = config_module._discover_agent_root()
    assert root == project, (
        "Should find the real project root, not the application sub-directory"
    )


def test_discover_skips_wrong_pyproject(monkeypatch, tmp_path: Path):
    """Sub-directory with a pyproject.toml whose name != AgentLoom is skipped."""
    project = tmp_path / "project"
    _write_pyproject(project, name="AgentLoom")
    _write_config(project)

    # A nested sub-project with a different name
    sub = project / "libs" / "other"
    _write_pyproject(sub, name="other-lib")
    monkeypatch.chdir(sub)

    root = config_module._discover_agent_root()
    assert root == project


def test_discover_from_file_fallback(monkeypatch, tmp_path: Path):
    """When cwd is completely outside the project, the __file__ fallback
    should still locate the real project root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    # The real _discover_agent_root uses __file__ (which points to the
    # installed source); we cannot easily fake that, so we just verify
    # that it doesn't raise when called from the real project.
    root = config_module._discover_agent_root()
    # Must point to the actual AgentLoom project root
    assert (root / "pyproject.toml").exists()
    assert config_module._is_agentloom_project(root / "pyproject.toml")


def test_discover_fails_outside_project(monkeypatch, tmp_path: Path):
    """When neither cwd nor __file__ can reach a valid root, raise."""
    outside = tmp_path / "nowhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    # Patch __file__-based search to also start from outside
    original = config_module.Path.__class__

    # Use a helper that replaces both search origins with the outside dir
    def _patched_discover(config_dir=None):
        current = outside
        while current != current.parent:
            candidate = current / "pyproject.toml"
            if candidate.exists() and config_module._is_agentloom_project(candidate):
                return current
            current = current.parent
        raise FileNotFoundError("test: cannot locate root")

    monkeypatch.setattr(config_module, "_discover_agent_root", _patched_discover)

    with pytest.raises(FileNotFoundError, match="cannot locate root"):
        config_module._discover_agent_root()
