from pathlib import Path

import yaml

import src.lib.config.config as config_module


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_system_fields_are_not_overridden_by_environment(monkeypatch, tmp_path):
    previous = config_module._ACTIVE_CONFIG
    try:
        agent_root = tmp_path / "agent"
        config_dir = agent_root / "config"

        _write_yaml(
            config_dir / "system.yaml",
            {
                "system": {
                    "name": "yaml-system-name",
                    "version": "9.9.9",
                    "user_agent": "yaml-agent/9.9.9",
                }
            },
        )
        _write_yaml(config_dir / "llm.yaml", {"model": {"default_model_type": "powerful"}})

        monkeypatch.setenv("SYSTEM_NAME", "env-system-name")
        monkeypatch.setenv("SYSTEM_VERSION", "1.2.3")
        monkeypatch.setenv("USER_AGENT", "env-agent/1.2.3")

        cfg = config_module._load_merged_config(config_dir=config_dir)
        config_module._ACTIVE_CONFIG = cfg

        assert cfg.system_name == "yaml-system-name"
        assert cfg.system_version == "9.9.9"
        assert cfg.user_agent == "yaml-agent/9.9.9"
        assert config_module.C.system_name == "yaml-system-name"
    finally:
        config_module._ACTIVE_CONFIG = previous
