"""验证 _resolve_app_root_from_yaml 和 build_effective_agent_config 的 app 覆盖发现逻辑。

app_root 确定规则：从 YAML 文件路径向上找第一个名为 workflows 的目录，
其父目录即为 app_root。若 app_root/config/system.yaml 存在则叠加覆盖。
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


# ─── 测试 _resolve_app_root_from_yaml ───


def test_yaml_in_workflows_finds_app_root(tmp_path):
    """YAML 在 workflows/ 下 → 正确返回 app_root。"""
    agent_root = tmp_path / "agent"
    app_root = agent_root / "applications" / "my_app"
    workflows_dir = app_root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = workflows_dir / "agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    result = config_module._resolve_app_root_from_yaml(agent_root, yaml_file)
    assert result == app_root


def test_yaml_in_worker_agents_finds_app_root(tmp_path):
    """YAML 在 workflows/worker_agents/ 深层 → 向上遍历命中 workflows/。"""
    agent_root = tmp_path / "agent"
    app_root = agent_root / "applications" / "my_app"
    worker_dir = app_root / "workflows" / "worker_agents"
    worker_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = worker_dir / "step0.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    result = config_module._resolve_app_root_from_yaml(agent_root, yaml_file)
    assert result == app_root


def test_nested_sub_app_hits_closest(tmp_path):
    """嵌套子 app（PduRCheck）的 YAML → 命中 PduRCheck 而非 ai_quality_analysis。"""
    agent_root = tmp_path / "agent"
    parent_app = agent_root / "applications" / "ai_quality_analysis"
    child_app = parent_app / "PduRCheck"

    # 父 app 和子 app 都有 workflows
    (parent_app / "workflows").mkdir(parents=True, exist_ok=True)
    child_workflows = child_app / "workflows"
    child_workflows.mkdir(parents=True, exist_ok=True)
    yaml_file = child_workflows / "agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    result = config_module._resolve_app_root_from_yaml(agent_root, yaml_file)
    assert result == child_app


def test_parent_app_yaml_hits_parent(tmp_path):
    """父 app（ai_quality_analysis）自身的 YAML → 命中 ai_quality_analysis 而非子 app。"""
    agent_root = tmp_path / "agent"
    parent_app = agent_root / "applications" / "ai_quality_analysis"
    child_app = parent_app / "PduRCheck"

    parent_workflows = parent_app / "workflows"
    parent_workflows.mkdir(parents=True, exist_ok=True)
    (child_app / "workflows").mkdir(parents=True, exist_ok=True)

    yaml_file = parent_workflows / "agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    result = config_module._resolve_app_root_from_yaml(agent_root, yaml_file)
    assert result == parent_app


def test_no_workflows_dir_raises_error(tmp_path):
    """无 workflows/ 目录 → 报 ValueError。"""
    agent_root = tmp_path / "agent"
    app_dir = agent_root / "applications" / "bad_app" / "some_dir"
    app_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = app_dir / "agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workflows/"):
        config_module._resolve_app_root_from_yaml(agent_root, yaml_file)


def test_yaml_outside_applications_returns_agent_root(tmp_path):
    """YAML 不在 applications/ 下 → 返回 agent_root。"""
    agent_root = tmp_path / "agent"
    scripts_dir = agent_root / "scripts" / "workflows"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = scripts_dir / "run.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    result = config_module._resolve_app_root_from_yaml(agent_root, yaml_file)
    assert result == agent_root


def test_no_applications_dir_returns_agent_root(tmp_path):
    """agent_root 下无 applications 目录 → workflows 找到但不在 applications 下 → 返回 agent_root。"""
    agent_root = tmp_path / "agent"
    workflows_dir = agent_root / "some_project" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = workflows_dir / "agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    result = config_module._resolve_app_root_from_yaml(agent_root, yaml_file)
    assert result == agent_root


# ─── 端到端测试 build_effective_agent_config ───


def test_build_effective_merges_app_overlay(tmp_path):
    """端到端：build_effective_agent_config 正确合并 app 级覆盖。"""
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    app_root = agent_root / "applications" / "my_app"

    _write_yaml(config_dir / "system.yaml", {
        "system": {"name": "base-system"},
        "tool_access_control": {"exclude_paths": ["Tools"]},
    })
    _write_yaml(config_dir / "llm.yaml", _minimal_llm_yaml())
    _write_yaml(app_root / "config" / "system.yaml", {
        "system": {"name": "app-system"},
        "tool_access_control": {"exclude_paths": ["Tools", "Test", ".git"]},
    })

    workflows_dir = app_root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    yaml_file = workflows_dir / "agent.yaml"
    yaml_file.write_text("name: test\n", encoding="utf-8")

    config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=config_dir)

    merged = config_module.build_effective_agent_config(
        {"_yaml_file_path": str(yaml_file)},
        source_name="test",
    )

    # app 覆盖生效
    assert merged.get("system", {}).get("name") == "app-system"
    assert ".git" in merged.get("tool_access_control", {}).get("exclude_paths", [])

    # 全局基础保留
    assert config_module.C.llm.for_type("powerful").model == "openai/test-model"


@pytest.mark.parametrize("key", ["runtime", "logging"])
def test_build_effective_rejects_global_only_app_overlay(tmp_path, key):
    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    app_root = agent_root / "applications" / "my_app"
    _write_yaml(config_dir / "system.yaml", {"system": {"name": "base"}})
    _write_yaml(config_dir / "llm.yaml", _minimal_llm_yaml())
    _write_yaml(app_root / "config" / "system.yaml", {key: {"root_dir": "other"}})
    yaml_file = app_root / "workflows" / "agent.yaml"
    _write_yaml(yaml_file, {"name": "test"})
    config_module._ACTIVE_CONFIG = config_module._load_merged_config(
        config_dir=config_dir
    )

    with pytest.raises(ValueError, match=rf"global-only.*{key}"):
        config_module.build_effective_agent_config(
            {"_yaml_file_path": str(yaml_file)},
            source_name="test",
        )
