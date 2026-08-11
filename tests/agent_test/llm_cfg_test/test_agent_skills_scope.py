import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.lib.smolagents.agent.base_agent as base_agent_module
from src.lib.smolagents.hooks import HookPlan, HookRun
from src.lib.smolagents.skills.catalog import SkillCatalog, SkillSource
from src.trace import get_current_hook_run, get_current_skill_catalog


class _DummyRuntimeAgent:
    def run(self, task: str, **kwargs):
        output = {
            "task": task,
            "skill_catalog": get_current_skill_catalog(),
            "hook_run": get_current_hook_run(required=True),
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


def _write_skill(root: Path, skill_name: str) -> Path:
    skill_dir = root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        f"---\nname: {skill_name}\ndescription: {skill_name}\n---\n# {skill_name}\n",
        encoding="utf-8",
    )
    return skill_path


def test_base_agent_run_binds_agent_scoped_catalogue():
    agent = _MinimalAgent(
        config={"name": "minimal_agent"},
        model=object(),
        logger=logging.getLogger(__name__),
    )
    custom_catalog = SkillCatalog.empty()
    agent._skill_catalog = custom_catalog
    agent._hook_plan = HookPlan()
    agent._effective_agent_config = {"tool_access_control": {}}
    agent.build_runtime_agent = lambda: _DummyRuntimeAgent()

    result = agent.run("demo-task")

    assert result["task"] == "demo-task"
    assert result["skill_catalog"] is custom_catalog
    assert isinstance(result["hook_run"], HookRun)
    assert result["hook_run"].plan is agent._hook_plan


def test_same_scope_duplicate_skill_names_fail(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first, "same-name")
    _write_skill(second, "same-name")

    with pytest.raises(ValueError, match="Duplicate skill name 'same-name'"):
        SkillCatalog.discover(
            [
                SkillSource(first, "application"),
                SkillSource(second, "application"),
            ]
        )


def test_agent_scope_overrides_application_scope(tmp_path: Path):
    application = tmp_path / "application"
    agent = tmp_path / "agent"
    _write_skill(application, "same-name")
    manifest = _write_skill(agent, "same-name")
    manifest.write_text(
        "---\nname: same-name\ndescription: agent copy\n---\n# agent body\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover(
        [SkillSource(application, "application"), SkillSource(agent, "agent")]
    )

    assert catalog.activate("same-name").instructions == "# agent body\n"
