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


def test_langfuse_app_override_and_defaults(tmp_path):
    """langfuse 配置只从 llm.yaml 读取，app system.yaml 中的 langfuse 键被过滤。"""
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"

    _write_yaml(
        config_dir / "system.yaml",
        {
            "model": {
                "default_model_type": "powerful",
                "powerful": {
                    "model": "openai/powerful",
                    "base_url": "https://powerful.example/v1",
                    "api_key": "powerful-key",
                    "requests_per_minute": 5,
                },
                "summary": {"model": "openai/test-summary"},
            },
        },
    )
    _write_yaml(
        config_dir / "llm.yaml",
        {
            "langfuse": {
                "enabled": True,
                "host": "https://langfuse-root.example",
                "public_key": "pk-root",
                "secret_key": "sk-root-secret",
            }
        },
    )

    cfg = config_module._load_merged_config(config_dir=config_dir)
    config_module._ACTIVE_CONFIG = cfg

    assert config_module.C.llm.langfuse.enabled is True
    assert config_module.C.llm.langfuse.host == "https://langfuse-root.example"
    assert config_module.C.llm.langfuse.public_key == "pk-root"
    assert config_module.C.llm.langfuse.private_key == "sk-root-secret"
