from pathlib import Path
import copy

import pytest

import src.lib.config.config as config_module


def _patch_base_config(monkeypatch, agent_root: Path) -> None:
    base_raw = {
        "tool_access_control": {"exclude_paths": ["Tools"]},
        "execution_env": {"type": "local"},
        "smart_summary": True,
    }
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(base_raw, agent_root=agent_root, llm_config=config_module.LLMConfig()),
        raising=True,
    )


def test_build_effective_agent_config_applies_workflow_overlay(monkeypatch, tmp_path: Path):
    agent_root = tmp_path / "agent"
    _patch_base_config(monkeypatch, agent_root)

    overlay = {
        "tool_access_control": {"exclude_paths": ["Test"]},
        "execution_env": {"type": "docker", "executor_kwargs": {"host": "127.0.0.1"}},
        "name": "supervisor_only_metadata",
        "workflow": "wf",
    }

    effective = config_module.build_effective_agent_config(overlay, source_name="supervisor.yaml")

    assert effective["tool_access_control"]["exclude_paths"] == ["Test"]
    assert effective["execution_env"]["type"] == "docker"
    assert effective["execution_env"]["executor_kwargs"]["host"] == "127.0.0.1"
    assert "name" not in effective
    assert "workflow" not in effective


def test_worker_effective_snapshot_is_independent_from_supervisor(monkeypatch, tmp_path: Path):
    agent_root = tmp_path / "agent"
    _patch_base_config(monkeypatch, agent_root)

    supervisor_cfg = {
        "tool_access_control": {"exclude_paths": ["Build"]},
        "execution_env": {"type": "docker", "executor_kwargs": {"host": "10.0.0.2"}},
    }
    worker_cfg = {
        "tool_access_control": {"exclude_paths": ["Temp"]},
        "execution_env": {"type": "local"},
    }

    supervisor_effective = config_module.build_effective_agent_config(supervisor_cfg, source_name="supervisor.yaml")
    worker_effective = config_module.build_effective_agent_config(worker_cfg, source_name="worker.yaml")

    assert supervisor_effective["tool_access_control"]["exclude_paths"] == ["Build"]
    assert worker_effective["tool_access_control"]["exclude_paths"] == ["Temp"]
    assert supervisor_effective["execution_env"]["type"] == "docker"
    assert worker_effective["execution_env"]["type"] == "local"


def test_build_effective_agent_config_rejects_project_key(monkeypatch, tmp_path: Path):
    agent_root = tmp_path / "agent"
    _patch_base_config(monkeypatch, agent_root)

    try:
        config_module.build_effective_agent_config(
            {"project": {"workspace_root": "legacy"}},
            source_name="legacy.yaml",
        )
    except ValueError as exc:
        assert "Use 'tool_access_control' instead" in str(exc)
    else:
        raise AssertionError("Expected ValueError for legacy project key")


def test_build_effective_agent_config_ignores_llm_only_keys_with_warning(
    monkeypatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    agent_root = tmp_path / "agent"
    _patch_base_config(monkeypatch, agent_root)
    caplog.set_level("WARNING")

    overlay = {
        "tool_access_control": {"exclude_paths": ["Vendor"]},
        "model": {"default_model_type": "fast"},
        "summary": {"model": "openai/test-summary"},
        "llm": {"provider": "x"},
        "langfuse": {"host": "https://lf.example"},
    }

    effective = config_module.build_effective_agent_config(overlay, source_name="supervisor.yaml")

    assert effective["tool_access_control"]["exclude_paths"] == ["Vendor"]
    assert "model" not in effective
    assert "llm" not in effective
    assert "langfuse" not in effective
    assert "Ignoring top-level key 'model'" in caplog.text
    assert "Ignoring top-level key 'llm'" in caplog.text
    assert "Ignoring top-level key 'langfuse'" in caplog.text


def test_build_effective_agent_config_does_not_mutate_base_snapshot(monkeypatch, tmp_path: Path):
    agent_root = tmp_path / "agent"
    _patch_base_config(monkeypatch, agent_root)
    base_before = copy.deepcopy(config_module.get_config().raw)

    _ = config_module.build_effective_agent_config(
        {
            "tool_access_control": {"exclude_paths": ["Build"]},
            "execution_env": {"type": "docker"},
        },
        source_name="worker.yaml",
    )

    base_after = config_module.get_config().raw
    assert base_before == base_after


def test_build_effective_agent_config_applies_top_level_smart_summary_override(monkeypatch, tmp_path: Path):
    agent_root = tmp_path / "agent"
    _patch_base_config(monkeypatch, agent_root)

    effective = config_module.build_effective_agent_config(
        {"smart_summary": False},
        source_name="worker.yaml",
    )

    assert effective["smart_summary"] is False
    assert config_module.get_config().raw["smart_summary"] is True
