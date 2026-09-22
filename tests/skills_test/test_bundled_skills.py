"""Validate shipped Skill assets through the actual runtime discovery seam."""

import shutil
from pathlib import Path

import pytest
from agentloom.application.definition import load_agent_definition, skill_sources
from agentloom.configuration.config import UnifiedConfig, build_effective_agent_config_snapshot
from agentloom.configuration.llm_config import LLMConfig
from agentloom.execution.skills.catalog import SkillCatalog, SkillSource

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOTS = sorted(
    [
        ROOT / "agentloom-framework-skill",
        *ROOT.glob("applications/*/skills"),
        *ROOT.glob("applications/*/agent-skills"),
    ]
)


@pytest.mark.parametrize("source", BUNDLED_ROOTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_bundled_skill_catalogs_are_discoverable_and_activatable(source: Path) -> None:
    catalog = SkillCatalog.discover([SkillSource(source, "application")])

    assert catalog.summaries(), f"No bundled Skills discovered under {source}"
    for summary in catalog.summaries():
        activation = catalog.activate(summary.name)
        assert activation.instructions.strip()
        assert activation.directory == summary.location.parent


def test_repo_map_guide_is_discovered_without_optional_application_config(tmp_path: Path) -> None:
    authored = ROOT / "applications/repo_map"
    application = tmp_path / "applications/nested/repo_map"
    workflow = application / "workflows/repo_map_agent.yaml"
    workflow.parent.mkdir(parents=True)
    shutil.copyfile(authored / "workflows/repo_map_agent.yaml", workflow)
    shutil.copytree(authored / "skills", application / "skills")
    base = UnifiedConfig({}, agent_root=tmp_path, llm_config=LLMConfig())

    snapshot = build_effective_agent_config_snapshot(load_agent_definition(workflow), base_config=base)
    catalog = SkillCatalog.discover(skill_sources(snapshot))

    assert not (application / "config/system.yaml").exists()
    assert [(item.name, item.scope) for item in catalog.summaries()] == [("repo-map-guide", "application")]
    activation = catalog.activate("repo-map-guide")
    assert activation.directory == application / "skills/repo-map-guide"
    assert "Repo Map 生成指南" in activation.instructions
