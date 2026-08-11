import logging
from pathlib import Path
from types import SimpleNamespace

from src.lib.smolagents.agent.base_agent import AgentRoleProfile, AgentType, RoleDrivenAgent
from src.lib.smolagents.prompts.prompt_builder import build_prompt_templates
from src.lib.smolagents.skills.catalog import SkillCatalog, SkillSource
from src.lib.smolagents.skills.parser import build_skills_prompt
from src.tools.skills import skill
from src.trace.task_context import clear_current_skill_catalog, set_current_skill_catalog


def _write_skill(root: Path, name: str, body: str = "# Exact instructions\n") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text("supporting evidence\n", encoding="utf-8")
    manifest = skill_dir / "SKILL.md"
    manifest.write_text(
        f"---\nname: {name}\ndescription: Use for {name} work.\n---\n{body}",
        encoding="utf-8",
    )
    return manifest


class _CatalogAgent(RoleDrivenAgent):
    def _role_profile(self) -> AgentRoleProfile:
        return AgentRoleProfile(agent_type=AgentType.WORKER, tool_call_type="tool_call")

    def _get_tools(self):
        return []


def test_catalogue_exposes_summaries_and_activation_adds_only_selected_body(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "release-review", "# Release-only instructions\n")
    _write_skill(skills_root, "incident-review", "# Incident-only instructions\n")

    catalog = SkillCatalog.discover([SkillSource(path=skills_root, scope="project")])

    summaries = catalog.summaries()
    prompt = build_skills_prompt(summaries)
    assert [(item.name, item.description) for item in summaries] == [
        ("incident-review", "Use for incident-review work."),
        ("release-review", "Use for release-review work."),
    ]
    assert "Release-only instructions" not in prompt
    assert "Incident-only instructions" not in prompt

    activation = catalog.activate("release-review")
    assert activation.instructions == "# Release-only instructions\n"
    assert activation.directory == skills_root / "release-review"
    assert activation.files == (skills_root / "release-review" / "references" / "guide.md",)


def test_skill_tool_injects_selected_instructions_and_resource_locations(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "release-review", "# Release-only instructions\n")
    _write_skill(skills_root, "incident-review", "# Incident-only instructions\n")
    catalog = SkillCatalog.discover([SkillSource(path=skills_root, scope="project")])
    set_current_skill_catalog(catalog)

    try:
        output = skill("release-review")
    finally:
        clear_current_skill_catalog()

    assert '<skill_content name="release-review">' in output
    assert "# Release-only instructions" in output
    assert "Incident-only instructions" not in output
    assert f"Base directory for this skill: {skills_root / 'release-review'}" in output
    assert str(skills_root / "release-review" / "references" / "guide.md") in output


def test_prompt_exposes_catalogue_only_when_skill_tool_is_available(monkeypatch, tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "release-review", "# Release-only instructions\n")
    catalog = SkillCatalog.discover([SkillSource(path=skills_root, scope="project")])
    prompt_path = tmp_path / "prompt.yaml"
    prompt_path.write_text("system_prompt: base\n", encoding="utf-8")

    import src.lib.smolagents.prompts.prompt_builder as prompt_builder_module

    monkeypatch.setattr(prompt_builder_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(prompt_builder_module, "get_agent_environment_prompt", lambda: "")

    hidden = build_prompt_templates(
        prompt_template_path=None,
        effective_prompt_path=None,
        model_id=None,
        agent_root=tmp_path,
        skill_catalog=catalog,
        skill_tool_enabled=False,
        logger=logging.getLogger(__name__),
    )
    visible = build_prompt_templates(
        prompt_template_path=None,
        effective_prompt_path=None,
        model_id=None,
        agent_root=tmp_path,
        skill_catalog=catalog,
        skill_tool_enabled=True,
        logger=logging.getLogger(__name__),
    )

    assert "release-review" not in hidden["system_prompt"]
    assert "release-review" in visible["system_prompt"]
    assert "Release-only instructions" not in visible["system_prompt"]


def test_runtime_catalog_composes_project_application_and_agent_sources(tmp_path: Path) -> None:
    project_root = tmp_path
    application_root = tmp_path / "applications" / "reports"
    _write_skill(project_root / "skills", "shared-review", "# Project instructions\n")
    _write_skill(application_root / "skills", "local-review", "# Application instructions\n")
    _write_skill(application_root / "agent-skills", "agent-review", "# Agent instructions\n")

    agent = object.__new__(_CatalogAgent)
    agent._config = {"name": "catalog-agent"}
    agent._effective_agent_config_snapshot = SimpleNamespace(
        layers=(
            SimpleNamespace(name="global_system", root=project_root, data={}),
            SimpleNamespace(name="application_system", root=application_root, data={}),
            SimpleNamespace(
                name="agent",
                root=application_root,
                data={"skills": {"paths": ["agent-skills"]}},
            ),
        )
    )

    catalog = agent.initialize_skill_catalog(logger=logging.getLogger(__name__))

    assert [(item.name, item.scope) for item in catalog.summaries()] == [
        ("agent-review", "agent"),
        ("local-review", "application"),
        ("shared-review", "project"),
    ]


def test_generated_skill_proposals_are_not_runtime_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "approved-skill")
    _write_skill(skills_root / "generated" / "proposals", "draft-skill")

    catalog = SkillCatalog.discover([SkillSource(skills_root, "project")])

    assert [item.name for item in catalog.summaries()] == ["approved-skill"]


def test_opencode_frontmatter_is_the_only_skill_metadata_contract(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "format-check"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: format-check
description: Validate the OpenCode metadata contract.
license: MIT
compatibility: AgentLoom >= 1.0
metadata:
  owner: runtime
allowed-tools: Bash
model: powerful
---
# Instructions
""",
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover([SkillSource(path=skill_dir, scope="project")])
    summary = catalog.summaries()[0]

    assert summary.name == "format-check"
    assert summary.description == "Validate the OpenCode metadata contract."
    assert not hasattr(summary, "allowed_tools")
    assert not hasattr(summary, "model")
