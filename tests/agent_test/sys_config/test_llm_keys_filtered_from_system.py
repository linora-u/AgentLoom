"""验证 system.yaml 中的 LLM 相关键会被 _filter_llm_only_top_level_keys 过滤掉。

system.yaml 只允许系统级配置（system, logging, tools, workspace 等），
所有 LLM 相关配置（model, llm, langfuse）必须且只能放在 llm.yaml 中。
"""

import logging
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
    """最小可用的 llm.yaml 配置。"""
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


def _setup_config_dir(tmp_path, system_yaml_data: dict, llm_yaml_data: dict | None = None):
    """创建临时 config 目录并写入 system.yaml 和 llm.yaml。"""
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    _write_yaml(config_dir / "system.yaml", system_yaml_data)
    _write_yaml(config_dir / "llm.yaml", llm_yaml_data or _minimal_llm_yaml())
    return config_dir


# ─── 测试：各 LLM 键在 system.yaml 中被单独过滤 ───


def test_system_yaml_model_key_is_filtered(tmp_path, monkeypatch):
    """system.yaml 中的 'model' 键应被过滤，不进入 merged config。"""
    config_dir = _setup_config_dir(tmp_path, {
        "model": {
            "default_model_type": "powerful",
            "common": {"base_url": "https://wrong.test/v1", "api_key": "wrong"},
            "model": "openai/test-common",
            "model": "openai/test-common",
            "powerful": {"model": "openai/should-be-ignored"},
            "summary": {"model": "openai/test-summary"},
        },
    })
    cfg = config_module._load_merged_config(config_dir=config_dir)

    # system.yaml 的 model 被过滤掉，实际 model 来自 llm.yaml
    assert cfg.llm.for_type("powerful").model == "openai/test-model"


def test_system_yaml_langfuse_key_is_filtered(tmp_path, monkeypatch):
    """system.yaml 中的 'langfuse' 键应被过滤，不影响 llm.yaml 的 langfuse。"""
    llm_data = _minimal_llm_yaml()
    llm_data["langfuse"] = {
        "enabled": True,
        "host": "https://langfuse-from-llm.example",
        "public_key": "pk-llm",
        "secret_key": "sk-llm",
    }
    config_dir = _setup_config_dir(
        tmp_path,
        system_yaml_data={
            "langfuse": {
                "enabled": False,
                "host": "https://langfuse-from-system.example",
                "public_key": "pk-system",
                "secret_key": "sk-system",
            },
        },
        llm_yaml_data=llm_data,
    )
    cfg = config_module._load_merged_config(config_dir=config_dir)

    # system.yaml 的 langfuse 被过滤，只有 llm.yaml 的生效
    assert cfg.llm.langfuse.host == "https://langfuse-from-llm.example"
    assert cfg.llm.langfuse.public_key == "pk-llm"


def test_system_yaml_llm_key_is_filtered(tmp_path, monkeypatch):
    """system.yaml 中的 'llm' 键应被过滤。"""
    config_dir = _setup_config_dir(tmp_path, {
        "llm": {"some_key": "some_value"},
    })
    cfg = config_module._load_merged_config(config_dir=config_dir)

    # 'llm' 键不会出现在 merged raw 中
    assert "llm" not in cfg.raw


def test_all_llm_keys_filtered_system_keys_preserved(tmp_path, monkeypatch):
    """system.yaml 同时包含 LLM 键和合法系统键时，LLM 键全部被过滤，系统键全部保留。"""
    config_dir = _setup_config_dir(tmp_path, {
        # LLM 键 — 应被过滤
        "model": {"default_model_type": "fast"},
        "summary": {"model": "openai/test-summary"},
        "llm": {"extra": True},
        "langfuse": {"enabled": False},
        # 合法系统键 — 应保留
        "system": {"name": "test-system", "version": "2.0.0"},
        "logging": {"level": "DEBUG"},
        "smart_summary": False,
    })
    cfg = config_module._load_merged_config(config_dir=config_dir)

    # 系统键保留
    assert cfg.system_name == "test-system"
    assert cfg.system_version == "2.0.0"
    assert cfg.raw.get("logging", {}).get("level") == "DEBUG"
    assert cfg.raw.get("smart_summary") is False

    # LLM 键不在 merged raw 中（它们从 system.yaml 路径被过滤了）
    assert "llm" not in cfg.raw


def test_filter_warns_on_llm_key_in_system_yaml(tmp_path, monkeypatch, caplog):
    """system.yaml 中存在 LLM 键时，应输出 warning 日志。"""
    config_dir = _setup_config_dir(tmp_path, {
        "model": {"default_model_type": "powerful"},
        "summary": {"model": "openai/test-summary"},
    })
    with caplog.at_level(logging.WARNING):
        config_module._load_merged_config(config_dir=config_dir)

    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("model" in msg and "config/system.yaml" in msg for msg in warning_messages), (
        f"Expected warning about 'model' in system.yaml, got: {warning_messages}"
    )
