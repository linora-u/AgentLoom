from __future__ import annotations

from pathlib import Path

import pytest

_DUPLICATE_HOOKS = """\
name: duplicate-hook-fixture
hooks:
  PreToolUse:
    - id: security.deny
      command: deny.py
  PreToolUse:
    - id: security.allow
      command: allow.py
"""


def test_system_config_rejects_duplicate_hook_event_keys(tmp_path: Path) -> None:
    from src.lib.config.config import _load_yaml

    path = tmp_path / "system.yaml"
    path.write_text(_DUPLICATE_HOOKS, encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate YAML mapping key: 'PreToolUse'"):
        _load_yaml(path)


def test_agent_yaml_rejects_duplicate_hook_event_keys(tmp_path: Path) -> None:
    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

    path = tmp_path / "agent.yaml"
    path.write_text(_DUPLICATE_HOOKS, encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate YAML mapping key: 'PreToolUse'"):
        YamlAgentFactory._load_config_from_file(path)
