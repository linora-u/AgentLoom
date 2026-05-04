"""验证应用级 system.yaml 覆盖也不能泄露 LLM 配置。

applications/<app>/config/system.yaml 只允许覆盖系统级配置，
LLM 相关键同样应被 _filter_llm_only_top_level_keys 过滤。

应用级覆盖通过 build_effective_agent_config 自动从 _yaml_file_path 发现。
"""

from pathlib import Path

import pytest
import yaml

import src.lib.config.config as config_module


@pytest.fixture(autouse=True)
def _restore_active_config():
    previous = config_module._ACTIVE_CONFIG
    try:
        yield
    finally:
        config_module._ACTIVE_CONFIG = previous


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _minimal_llm_yaml() -> dict:
    return {
        "model": {
            "default_model_type": "powerful",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
                "requests_per_minute": 10,
            },
            "powerful": {"model": "openai/test-model"},
            "summary": {"model": "openai/test-summary"},
        },
    }


def _setup_with_app_override(tmp_path, system_yaml_data, app_system_yaml_data, llm_yaml_data=None):
    """创建包含 app 级覆盖的 config 目录结构（含 workflows 目录）。"""
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    app_root = agent_root / "applications" / "test_app"

    _write_yaml(config_dir / "system.yaml", system_yaml_data)
    _write_yaml(config_dir / "llm.yaml", llm_yaml_data or _minimal_llm_yaml())
    _write_yaml(app_root / "config" / "system.yaml", app_system_yaml_data)

    workflows_dir = app_root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = workflows_dir / "test_agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    return config_dir, yaml_file


def _build_effective(config_dir, yaml_file):
    """加载基础配置并构建 effective config。"""
    config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=config_dir)
    agent_config = {"_yaml_file_path": str(yaml_file)}
    return config_module.build_effective_agent_config(agent_config, source_name="test")


# ─── 测试：app 级 system.yaml 中 LLM 键被过滤 ───


def test_app_system_yaml_model_key_filtered(tmp_path):
    """应用级 system.yaml 中的 'model' 键应被过滤。"""
    config_dir, yaml_file = _setup_with_app_override(
        tmp_path,
        system_yaml_data={},
        app_system_yaml_data={
            "model": {
                "default_model_type": "fast",
                "common": {"base_url": "https://wrong.test/v1"},
                "model": "openai/test-common",
                "model": "openai/test-common",
                "powerful": {"model": "openai/should-be-ignored"},
                "summary": {"model": "openai/test-summary"},
            },
        },
    )
    _build_effective(config_dir, yaml_file)

    assert config_module.C.llm.for_type("powerful").model == "openai/test-model"


def test_app_system_yaml_langfuse_key_filtered(tmp_path):
    """应用级 system.yaml 中的 'langfuse' 键应被过滤。"""
    llm_data = _minimal_llm_yaml()
    llm_data["langfuse"] = {
        "enabled": True,
        "host": "https://langfuse-from-llm.example",
    }
    config_dir, yaml_file = _setup_with_app_override(
        tmp_path,
        system_yaml_data={},
        app_system_yaml_data={
            "langfuse": {
                "enabled": False,
                "host": "https://langfuse-from-app.example",
            },
        },
        llm_yaml_data=llm_data,
    )
    _build_effective(config_dir, yaml_file)

    assert config_module.C.llm.langfuse.host == "https://langfuse-from-llm.example"
    assert config_module.C.llm.langfuse.enabled is True


def test_app_override_preserves_workspace(tmp_path):
    """app 级 system.yaml 可以合法覆盖 workspace，但其中的 LLM 键被过滤。"""
    config_dir, yaml_file = _setup_with_app_override(
        tmp_path,
        system_yaml_data={
            "system": {"name": "base-system"},
        },
        app_system_yaml_data={
            "tool_access_control": {
                "exclude_paths": ["Tools", "Test", ".git", "build"],
            },
            "system": {"name": "app-system"},
            "model": {"default_model_type": "fast"},
            "summary": {"model": "openai/test-summary"},
        },
    )
    merged = _build_effective(config_dir, yaml_file)

    assert ".git" in merged.get("tool_access_control", {}).get("exclude_paths", [])
    assert "build" in merged.get("tool_access_control", {}).get("exclude_paths", [])
    assert merged.get("system", {}).get("name") == "app-system"
    assert config_module.C.llm.for_type("powerful").model == "openai/test-model"
