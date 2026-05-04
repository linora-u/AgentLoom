import os
from pathlib import Path

import src.lib.config.config as config_module
import src.lib.utils.workspace as workspace_module


def _patch_config(monkeypatch, raw: dict, root: Path) -> None:
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(raw, agent_root=root, llm_config=config_module.LLMConfig()),
        raising=True,
    )


def test_resolve_workspace_root_returns_agent_root(monkeypatch, tmp_path: Path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True)
    _patch_config(monkeypatch, {"tool_access_control": {}}, agent_root)

    resolved = workspace_module.resolve_workspace_root()

    assert resolved == agent_root.resolve()


def test_ensure_workspace_mounted_once_is_idempotent(monkeypatch, tmp_path: Path):
    previous_cfg = config_module._ACTIVE_CONFIG
    previous_cwd = Path.cwd().resolve()
    previous_last_mounted = workspace_module._LAST_MOUNTED_WORKSPACE
    previous_original_cwd = workspace_module._ORIGINAL_CWD
    real_chdir = workspace_module.os.chdir

    try:
        agent_root = tmp_path / "agent"
        agent_root.mkdir(parents=True)
        _patch_config(monkeypatch, {"tool_access_control": {}}, agent_root)

        monkeypatch.setattr(workspace_module, "_LAST_MOUNTED_WORKSPACE", None, raising=True)
        monkeypatch.setattr(workspace_module, "_ORIGINAL_CWD", None, raising=True)

        chdir_calls: list[Path] = []

        def _track_chdir(path: str | os.PathLike[str]) -> None:
            chdir_calls.append(Path(path).resolve())
            real_chdir(path)

        monkeypatch.setattr(workspace_module.os, "chdir", _track_chdir)

        workspace_module.ensure_workspace_mounted_once()
        workspace_module.ensure_workspace_mounted_once()

        assert chdir_calls == [agent_root.resolve()]
        assert Path.cwd().resolve() == agent_root.resolve()
    finally:
        os.chdir(previous_cwd)
        config_module._ACTIVE_CONFIG = previous_cfg
        workspace_module._LAST_MOUNTED_WORKSPACE = previous_last_mounted
        workspace_module._ORIGINAL_CWD = previous_original_cwd
