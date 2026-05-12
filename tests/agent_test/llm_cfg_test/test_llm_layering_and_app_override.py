"""验证三层配置分层合并 + LLM 只从 llm.yaml 读取。

合并顺序: config/system.yaml -> config/llm.yaml -> app/config/system.yaml -> agent overlay
LLM 键（model/langfuse）在 system.yaml 和 app/system.yaml 中都会被过滤。
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


def _setup_layered(tmp_path, *, system_data, llm_data, app_data=None, app_name="ai_quality_analysis/OTA"):
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    _write_yaml(config_dir / "system.yaml", system_data)
    _write_yaml(config_dir / "llm.yaml", llm_data)

    app_root = agent_root / "applications" / app_name
    workflows_dir = app_root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = workflows_dir / "test_agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    if app_data is not None:
        _write_yaml(app_root / "config" / "system.yaml", app_data)

    return config_dir, yaml_file


def _build_effective(config_dir, yaml_file):
    config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=config_dir)
    agent_config = {"_yaml_file_path": str(yaml_file)}
    return config_module.build_effective_agent_config(agent_config, source_name="test")


def test_layering_precedence_with_application_override(tmp_path):
    config_dir, yaml_file = _setup_layered(
        tmp_path,
        system_data={
            "tool_access_control": {"exclude_paths": ["Tools"]},
            "prompt": {"path": "prompts/system_prompt.yaml"},
            "model": {
                "default_model_type": "powerful",
                "powerful": {"model": "openai/system-powerful", "base_url": "https://system.example/v1", "api_key": "system-key", "requests_per_minute": 3, "timeout": 50},
                "summary": {"model": "openai/test-summary"},
            },
        },
        llm_data={
            "model": {
                "default_model_type": "powerful",
                "powerful": {"model": "openai/llm-powerful", "base_url": "https://llm.example/v1", "api_key": "llm-key", "requests_per_minute": 9, "timeout": 60},
                "summary": {"model": "openai/test-summary"},
            }
        },
        app_data={
            "tool_access_control": {"exclude_paths": ["Tools", ".git"]},
            "prompt": {"path": "prompts/app_prompt.yaml"},
            "model": {"powerful": {"model": "openai/app-powerful", "api_key": "app-key"}},
            "summary": {"model": "openai/test-summary"},
            "langfuse": {"host": "https://app-langfuse.example"},
        },
    )
    merged = _build_effective(config_dir, yaml_file)

    assert merged.get("prompt", {}).get("path") == "prompts/app_prompt.yaml"

    powerful = config_module.C.llm.for_type("powerful")
    assert powerful.model == "openai/llm-powerful"
    assert powerful.base_url == "https://llm.example/v1"
    assert powerful.api_key == "llm-key"
    assert powerful.requests_per_minute == 9
    assert powerful.timeout == 60


def test_non_application_path_does_not_load_app_override(tmp_path):
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    _write_yaml(config_dir / "system.yaml", {"prompt": {"path": "prompts/system_prompt.yaml"}})
    _write_yaml(config_dir / "llm.yaml", {
        "model": {
            "default_model_type": "powerful",
            "powerful": {"model": "openai/llm-powerful", "base_url": "https://llm.example/v1"},
            "summary": {"model": "openai/test-summary"},
        }
    })
    app_root = agent_root / "applications" / "ai_quality_analysis"
    _write_yaml(app_root / "config" / "system.yaml", {"prompt": {"path": "prompts/app_prompt.yaml"}})

    config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=config_dir)
    scripts_dir = agent_root / "scripts" / "workflows"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = scripts_dir / "run.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    merged = config_module.build_effective_agent_config(
        {"_yaml_file_path": str(yaml_file)}, source_name="test"
    )

    assert merged.get("prompt", {}).get("path") == "prompts/system_prompt.yaml"
    assert config_module.C.llm.for_type("powerful").model == "openai/llm-powerful"


def test_max_tokens_uses_llm_yaml_only_and_ignores_system_layers(tmp_path):
    config_dir, yaml_file = _setup_layered(
        tmp_path,
        system_data={
            "model": {"default_model_type": "powerful", "powerful": {"model": "openai/system-powerful", "max_tokens": 22222}, "summary": {"model": "openai/test-summary"}},
        },
        llm_data={
            "model": {"default_model_type": "powerful", "powerful": {"model": "openai/llm-powerful", "max_tokens": 44444}, "summary": {"model": "openai/test-summary"}},
        },
        app_data={
            "model": {"powerful": {"max_tokens": 66666}, "summary": {"model": "openai/test-summary"}},
        },
    )
    _build_effective(config_dir, yaml_file)

    assert config_module.C.llm.for_type("powerful").max_tokens == 44444


def test_max_tokens_falls_back_to_builtin_when_llm_yaml_omits_it(tmp_path):
    config_dir, yaml_file = _setup_layered(
        tmp_path,
        system_data={
            "model": {"default_model_type": "powerful", "powerful": {"model": "openai/system-powerful", "max_tokens": 33333}, "summary": {"model": "openai/test-summary"}},
        },
        llm_data={
            "model": {"default_model_type": "powerful", "powerful": {"model": "openai/llm-powerful"}, "summary": {"model": "openai/test-summary"}},
        },
        app_data={
            "model": {"powerful": {"max_tokens": 55555}, "summary": {"model": "openai/test-summary"}},
        },
    )
    _build_effective(config_dir, yaml_file)

    assert config_module.C.llm.for_type("powerful").max_tokens == 150000
