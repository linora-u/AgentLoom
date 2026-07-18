import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.lib.smolagents.agent.base_agent as base_agent_module
import src.lib.smolagents.prompts.prompt_builder as prompt_builder_module
from src.lib.smolagents.hooks import HookPlan, HookRun
from src.lib.smolagents.skills.skills import SkillsManager
from src.trace import (
    get_current_hook_run,
    get_current_skills_manager,
)


class _DummyConfig:
    def __init__(self, agent_root: Path, system_skills=None):
        self.agent_root = agent_root
        self._system_skills = system_skills

    def get(self, key: str, default=None):
        if key == "skills":
            return self._system_skills
        if key == "tools":
            return {
                "tools_mapping": {
                    "Claude": {
                        "Read": "read_file",
                        "Write": "write_file",
                        "Bash": "shell_tool",
                    }
                }
            }
        return default


class _DummyRuntimeAgent:
    def run(self, task: str, **kwargs):
        skills_manager = get_current_skills_manager()
        hook_run = get_current_hook_run(required=True)
        output = {
            "task": task,
            "skills_manager": skills_manager,
            "hook_run": hook_run,
        }
        if kwargs.get("return_full_result"):
            return SimpleNamespace(output=output, state="success")
        return output


class _MinimalAgent(base_agent_module.RoleDrivenAgent):
    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.WORKER,
            tool_call_type="code_act",
        )

    def _get_tools(self):
        return []


class _DummyRoleAgent(base_agent_module.RoleDrivenAgent):
    def _role_profile(self) -> base_agent_module.AgentRoleProfile:
        return base_agent_module.AgentRoleProfile(
            agent_type=base_agent_module.AgentType.WORKER,
            tool_call_type="tool_call",
        )

    def _get_tools(self):
        return []


def _write_skill(root: Path, folder_name: str, skill_name: str) -> Path:
    skill_dir = root / folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "skill.md"
    skill_path.write_text(
        f"---\nname: {skill_name}\ndescription: {skill_name}\n---\n# {skill_name}\n",
        encoding="utf-8",
    )
    return skill_path


@pytest.fixture(autouse=True)
def reset_skill_singletons():
    SkillsManager._instance = None
    yield
    SkillsManager._instance = None


def test_base_agent_run_binds_agent_scoped_managers():
    agent = _MinimalAgent(
        config={"name": "minimal_agent"},
        model=object(),
        logger=logging.getLogger(__name__),
    )
    custom_skills = SkillsManager(logger=logging.getLogger(__name__))
    agent._skills_manager = custom_skills
    agent._hook_plan = HookPlan()
    agent._effective_agent_config = {"tool_access_control": {}}

    # Monkey-patch build_runtime_agent so run() uses our dummy runner
    agent.build_runtime_agent = lambda: _DummyRuntimeAgent()

    result = agent.run("demo-task")

    assert result["task"] == "demo-task"
    assert result["skills_manager"] is custom_skills
    assert isinstance(result["hook_run"], HookRun)
    assert result["hook_run"].plan is agent._hook_plan


def test_initialize_skills_manager_loads_system_root_and_agent_layers(monkeypatch, tmp_path: Path):
    system_root = tmp_path / "global_cfg"
    default_root = tmp_path / "skills"
    agent_root = tmp_path / "agent_local"

    _write_skill(system_root, "sys_skill", "sys-skill")
    _write_skill(default_root, "default_skill", "default-skill")
    _write_skill(agent_root, "agent_skill", "agent-skill")

    config = _DummyConfig(
        agent_root=tmp_path,
        system_skills=[{"path": "global_cfg", "platform": "Claude"}],
    )
    monkeypatch.setattr(base_agent_module, "C", config)

    agent = object.__new__(_DummyRoleAgent)
    agent._skills_manager = SkillsManager(logger=logging.getLogger(__name__))
    agent._effective_agent_config = {
        "skills": [{"path": "global_cfg", "platform": "Claude"}],
    }

    agent.initialize_skills_manager(
        {"skills": [{"path": "agent_local", "platform": "Claude"}]},
        logger=logging.getLogger(__name__),
    )

    assert set(agent._skills_manager.skills) == {"sys-skill", "default-skill", "agent-skill"}


def test_skill_group_enable_hooks_is_a_migration_error(monkeypatch, tmp_path: Path):
    _write_skill(tmp_path, "pure_skill", "pure-skill")
    monkeypatch.setattr(base_agent_module, "C", _DummyConfig(agent_root=tmp_path))

    agent = object.__new__(_DummyRoleAgent)
    agent._skills_manager = SkillsManager(logger=logging.getLogger(__name__))
    agent._effective_agent_config = {
        "skills": {
            "enable-hooks": True,
            "items": [{"path": "pure_skill"}],
        }
    }

    with pytest.raises(ValueError, match="skills.enable-hooks"):
        agent.initialize_skills_manager({}, logger=logging.getLogger(__name__))


def test_skill_item_enable_hooks_is_a_migration_error(monkeypatch, tmp_path: Path):
    _write_skill(tmp_path, "pure_skill", "pure-skill")
    monkeypatch.setattr(base_agent_module, "C", _DummyConfig(agent_root=tmp_path))

    agent = object.__new__(_DummyRoleAgent)
    agent._skills_manager = SkillsManager(logger=logging.getLogger(__name__))
    agent._effective_agent_config = {}

    with pytest.raises(ValueError, match="skills.items.enable-hooks"):
        agent.initialize_skills_manager(
            {"skills": [{"path": "pure_skill", "enable-hooks": True}]},
            logger=logging.getLogger(__name__),
        )


def test_build_prompt_templates_uses_agent_scoped_skills_manager(monkeypatch, tmp_path: Path):
    prompt_path = tmp_path / "prompt.yaml"
    prompt_path.write_text("system_prompt: base", encoding="utf-8")

    skill_path = _write_skill(tmp_path / "prompt_skills", "one", "scoped-skill")

    agent = _MinimalAgent(
        config={"name": "minimal_agent"},
        model=object(),
        logger=logging.getLogger(__name__),
    )
    agent._skills_manager = SkillsManager(logger=logging.getLogger(__name__))
    agent._skills_manager.load_skill_metadata(str(skill_path))

    class _NoSkills:
        def get_eager_skills_prompt(self):
            return ""

        def get_skills_prompt(self):
            return ""

    monkeypatch.setattr(prompt_builder_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")
    monkeypatch.setattr(prompt_builder_module.SkillsManager, "get_instance", lambda logger=None: _NoSkills())

    templates = agent._build_prompt_templates(
        runtime_logger=logging.getLogger(__name__),
        use_customized_prompt=True,
        prompt_template_path=None,
    )

    assert "scoped-skill" in templates["system_prompt"]
