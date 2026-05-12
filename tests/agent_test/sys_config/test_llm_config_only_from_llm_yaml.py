"""验证 LLM 配置只从 llm.yaml 生效。

system.yaml 中写入的 LLM 配置不会影响最终的 LLMConfig，
所有 model/langfuse 配置必须且只能通过 config/llm.yaml 提供。
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


def _setup_config_dir(tmp_path, system_yaml_data: dict, llm_yaml_data: dict):
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    _write_yaml(config_dir / "system.yaml", system_yaml_data)
    _write_yaml(config_dir / "llm.yaml", llm_yaml_data)
    return config_dir


# ─── 测试：LLM 配置只从 llm.yaml 读取 ───


def test_model_config_loaded_from_llm_yaml(tmp_path, monkeypatch):
    """llm.yaml 中的 model 配置应正确加载到 C.llm。"""
    config_dir = _setup_config_dir(
        tmp_path,
        system_yaml_data={},
        llm_yaml_data={
            "model": {
                "default_model_type": "powerful",
                "powerful": {
                    "model": "openai/gpt-5",
                    "base_url": "https://llm-from-llm-yaml.test/v1",
                    "api_key": "llm-yaml-key",
                    "requests_per_minute": 15,
                },
                "fast": {
                    "model": "openai/gpt-4o-mini",
                    "base_url": "https://fast.test/v1",
                    "api_key": "fast-key",
                },
                "summary": {"model": "openai/test-summary"},
            },
        },
    )
    cfg = config_module._load_merged_config(config_dir=config_dir)

    assert cfg.llm.for_type("powerful").model == "openai/gpt-5"
    assert cfg.llm.for_type("fast").model == "openai/gpt-4o-mini"
    assert cfg.llm.for_type("powerful").base_url == "https://llm-from-llm-yaml.test/v1"
    assert cfg.llm.for_type("powerful").api_key == "llm-yaml-key"
    assert cfg.llm.for_type("powerful").requests_per_minute == 15
    assert set(cfg.llm.available_types) == {"powerful", "fast", "summary"}


def test_langfuse_loaded_from_llm_yaml(tmp_path, monkeypatch):
    """llm.yaml 中的 langfuse 配置应正确加载。"""
    config_dir = _setup_config_dir(
        tmp_path,
        system_yaml_data={},
        llm_yaml_data={
            "model": {
                "default_model_type": "powerful",
                "powerful": {"model": "openai/test", "base_url": "https://test/v1", "api_key": "k"},
                "summary": {"model": "openai/test-summary"},
            },
            "langfuse": {
                "enabled": True,
                "host": "https://langfuse.custom.example",
                "public_key": "pk-custom",
                "secret_key": "sk-custom",
            },
        },
    )
    cfg = config_module._load_merged_config(config_dir=config_dir)

    assert cfg.llm.langfuse.enabled is True
    assert cfg.llm.langfuse.host == "https://langfuse.custom.example"
    assert cfg.llm.langfuse.public_key == "pk-custom"
    assert cfg.llm.langfuse.private_key == "sk-custom"


def test_model_in_system_yaml_ignored_llm_yaml_wins(tmp_path, monkeypatch):
    """system.yaml 中的 model 配置被忽略，llm.yaml 的配置生效。"""
    config_dir = _setup_config_dir(
        tmp_path,
        system_yaml_data={
            # 这些 LLM 键会被 _filter_llm_only_top_level_keys 过滤
            "model": {
                "default_model_type": "fast",
                "powerful": {"model": "openai/wrong-model", "base_url": "https://wrong.test/v1", "api_key": "wrong-key"},
                "summary": {"model": "openai/test-summary"},
            },
            "langfuse": {
                "enabled": False,
                "host": "https://wrong-langfuse.test",
            },
        },
        llm_yaml_data={
            "model": {
                "default_model_type": "powerful",
                "powerful": {"model": "openai/correct-model", "base_url": "https://correct.test/v1", "api_key": "correct-key"},
                "summary": {"model": "openai/test-summary"},
            },
            "langfuse": {
                "enabled": True,
                "host": "https://correct-langfuse.test",
            },
        },
    )
    cfg = config_module._load_merged_config(config_dir=config_dir)

    # llm.yaml 的配置生效
    assert cfg.llm.default_model_type == "powerful"
    assert cfg.llm.for_type("powerful").model == "openai/correct-model"
    assert cfg.llm.for_type("powerful").base_url == "https://correct.test/v1"
    assert cfg.llm.for_type("powerful").api_key == "correct-key"
    assert cfg.llm.langfuse.enabled is True
    assert cfg.llm.langfuse.host == "https://correct-langfuse.test"


def test_llm_yaml_missing_fields_use_defaults(tmp_path, monkeypatch):
    """llm.yaml 不写 langfuse 时，应回退到默认值。"""
    config_dir = _setup_config_dir(
        tmp_path,
        system_yaml_data={},
        llm_yaml_data={
            "model": {
                "default_model_type": "powerful",
                "powerful": {"model": "openai/test", "base_url": "https://test/v1", "api_key": "k"},
                "summary": {"model": "openai/test-summary"},
            },
            # 不写 langfuse — 应使用默认值
        },
    )
    cfg = config_module._load_merged_config(config_dir=config_dir)

    # langfuse 回退到 LangfuseSettings 默认值
    assert cfg.llm.langfuse.enabled is True
    assert cfg.llm.langfuse.host == "https://cloud.langfuse.com"
    assert cfg.llm.langfuse.public_key == ""
    assert cfg.llm.langfuse.private_key == ""
