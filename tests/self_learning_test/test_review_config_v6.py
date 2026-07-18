from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write_yaml(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _llm_yaml() -> dict:
    return {
        "model": {
            "default_model_type": "summary",
            "common": {
                "model": "openai/test-common",
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
            },
            "summary": {"model": "openai/test-summary"},
        }
    }


def _root_review_config() -> dict:
    return {
        "self_learning": {
            "review": {
                "enabled": True,
                "application": {"review_model": "summary"},
                "project": {"review_model": "summary"},
            }
        }
    }


def test_review_config_exposes_application_and_project_policies() -> None:
    from src.extensions.self_learning.paths import review_config

    config = {
        "self_learning": {
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "summary",
                    "trigger": {"mode": "batch", "min_completed_runs": 5},
                    "approval": {"fact": "auto", "experience": "manual"},
                },
                "project": {
                    "review_model": "summary",
                    "trigger": {"mode": "batch", "min_candidates": 7},
                    "approval": {"fact": "manual", "experience": "auto"},
                },
                "artifacts": {
                    "markdown": False,
                    "review_auto_applied": True,
                },
            }
        }
    }

    assert review_config(config, scope="application") == {
        "enabled": True,
        "review_model": "summary",
        "trigger": {"mode": "batch", "min_completed_runs": 5},
        "approval": {"fact": "auto", "experience": "manual"},
        "artifacts": {"markdown": False, "review_auto_applied": True},
    }
    assert review_config(config, scope="project") == {
        "enabled": True,
        "review_model": "summary",
        "trigger": {"mode": "batch", "min_candidates": 7},
        "approval": {"fact": "manual", "experience": "auto"},
        "artifacts": {"markdown": False, "review_auto_applied": True},
    }


def test_review_config_rejects_unknown_trigger_mode() -> None:
    from src.lib.config import validate_system_snapshot

    with pytest.raises(ValueError, match="trigger.mode"):
        validate_system_snapshot(
            {
                "self_learning": {
                    "review": {
                        "enabled": True,
                        "application": {
                            "review_model": "summary",
                            "trigger": {"mode": "eventually"},
                        },
                        "project": {"review_model": "summary"},
                    }
                }
            },
            "test config",
        )


def test_legacy_memory_review_keys_fail_with_migration_guidance() -> None:
    from src.lib.config import validate_system_snapshot

    with pytest.raises(
        ValueError,
        match=r"self_learning\.memory\.review_model.*self_learning\.review\.application\.review_model",
    ):
        validate_system_snapshot(
            {
                "self_learning": {
                    "memory": {
                        "review_model": "summary",
                        "write_approval": True,
                    }
                }
            },
            "legacy config",
        )


def test_application_config_cannot_override_project_review_policy(tmp_path) -> None:
    import src.lib.config.config as config_module

    agent_root = tmp_path / "agent"
    config_dir = agent_root / "config"
    app_root = agent_root / "applications" / "orders"
    workflow = app_root / "workflows" / "agent.yaml"
    _write_yaml(config_dir / "system.yaml", _root_review_config())
    _write_yaml(config_dir / "llm.yaml", _llm_yaml())
    _write_yaml(
        app_root / "config" / "system.yaml",
        {
            "self_learning": {
                "review": {
                    "application": {"approval": {"experience": "auto"}},
                    "project": {"approval": {"experience": "auto"}},
                }
            }
        },
    )
    _write_yaml(workflow, {"name": "orders"})

    previous = config_module._ACTIVE_CONFIG
    try:
        config_module._ACTIVE_CONFIG = config_module._load_merged_config(config_dir=config_dir)
        with pytest.raises(ValueError, match=r"project review policy.*project root"):
            config_module.build_effective_agent_config(
                {"_yaml_file_path": str(workflow)},
                source_name="orders workflow",
            )
    finally:
        config_module._ACTIVE_CONFIG = previous


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("enabled", False),
        ("artifacts", {"markdown": False}),
        ("project", {"approval": {"fact": "auto"}}),
        ("future_global_policy", {}),
    ],
)
def test_application_review_overlay_allows_only_application_policy(key, value) -> None:
    from src.lib.config.config import extract_workflow_overlay

    with pytest.raises(
        ValueError,
        match=r"only self_learning\.review\.application",
    ):
        extract_workflow_overlay(
            {"self_learning": {"review": {key: value}}},
            source_name="application agent.yaml",
        )


def test_enabled_review_rejects_unconfigured_model(tmp_path) -> None:
    import src.lib.config.config as config_module

    config_dir = tmp_path / "agent" / "config"
    invalid = _root_review_config()
    invalid["self_learning"]["review"]["project"]["review_model"] = "missing"
    _write_yaml(config_dir / "system.yaml", invalid)
    _write_yaml(config_dir / "llm.yaml", _llm_yaml())

    with pytest.raises(
        ValueError,
        match=r"self_learning\.review\.project\.review_model.*missing.*config/llm.yaml",
    ):
        config_module._load_merged_config(config_dir=config_dir)


def test_project_system_yaml_declares_safe_v6_review_defaults() -> None:
    from src.lib.config import validate_system_snapshot

    raw = yaml.safe_load(Path("config/system.yaml").read_text(encoding="utf-8"))
    validate_system_snapshot(raw, "config/system.yaml")

    review = raw["self_learning"]["review"]
    assert review == {
        "enabled": True,
        "application": {
            "review_model": "summary",
            "trigger": {"mode": "batch", "min_completed_runs": 5},
            "approval": {"fact": "auto", "experience": "manual"},
        },
        "project": {
            "review_model": "summary",
            "trigger": {"mode": "batch", "min_candidates": 5},
            "approval": {"fact": "manual", "experience": "manual"},
        },
        "artifacts": {"markdown": True, "review_auto_applied": True},
    }
