from pathlib import Path

import src.lib.smolagents.agent.agent_env as agent_env_module


class _DummyConfig:
    def __init__(self, workspace_cfg: dict, agent_root: Path):
        self._workspace_cfg = workspace_cfg
        self.agent_root = agent_root

    def get(self, key, default=None):
        if key == "tool_access_control":
            return self._workspace_cfg
        return default


def _write_prompt_template(path: Path) -> None:
    path.write_text(
        "template: |+\n"
        "  Root: {workspace_root}\n"
        "  Excluded:\n"
        "  {exclude_section}\n",
        encoding="utf-8",
    )


def test_environment_prompt_renders_excluded_paths_only(monkeypatch, tmp_path):
    template_path = tmp_path / "environment_prompt.yaml"
    _write_prompt_template(template_path)

    monkeypatch.setattr(agent_env_module, "_PROMPT_TEMPLATE_PATH", template_path)
    monkeypatch.setattr(
        agent_env_module,
        "C",
        _DummyConfig(
            workspace_cfg={"path_validation": [
                {"tools": ["read_file"], "exclude_paths": ["Tools", "Test"]}
            ]},
            agent_root=tmp_path,
        ),
    )

    prompt = agent_env_module.get_agent_environment_prompt()

    assert str(tmp_path.resolve()) in prompt
    assert "[RESTRICTED]" in prompt
    assert "Tools" in prompt
    assert "Test" in prompt
    assert "ACCESSIBLE" not in prompt


def test_environment_prompt_shows_default_when_exclude_paths_empty(monkeypatch, tmp_path):
    template_path = tmp_path / "environment_prompt.yaml"
    _write_prompt_template(template_path)

    monkeypatch.setattr(agent_env_module, "_PROMPT_TEMPLATE_PATH", template_path)
    monkeypatch.setattr(
        agent_env_module,
        "C",
        _DummyConfig(
            workspace_cfg={"exclude_paths": []},
            agent_root=tmp_path,
        ),
    )

    prompt = agent_env_module.get_agent_environment_prompt()

    assert "[OPEN] No excluded directories configured." in prompt
