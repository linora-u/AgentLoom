"""End-to-end tests for skill prompt assembly.

Configured skills are either:
  - on-demand: lightweight catalogue only, then model calls load_skill
  - eager: full skill body is injected into the system prompt
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

import src.lib.smolagents.prompts.prompt_builder as pb_module
from src.lib.smolagents.prompts.prompt_builder import build_prompt_templates
from src.lib.smolagents.skills.skills import SkillsManager

_LOGGER = logging.getLogger(__name__)


def _make_prompt_yaml(tmp_path: Path) -> Path:
    prompt_file = tmp_path / "code_agent.yaml"
    prompt_file.write_text("system_prompt: base", encoding="utf-8")
    return prompt_file


def _make_skill(
    parent_dir: Path,
    name: str,
    description: str = "A test skill",
    extra_frontmatter: str = "",
) -> Path:
    skill_dir = parent_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if extra_frontmatter:
        lines.extend(extra_frontmatter.strip().splitlines())
    lines.extend(
        [
            "---",
            f"# {name} instructions",
            f"Body of {name}.",
            "",
        ]
    )
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("\n".join(lines), encoding="utf-8")
    return skill_path


def _fresh_skills_manager() -> SkillsManager:
    SkillsManager._instance = None
    return SkillsManager(logger=logging.getLogger(__name__))


def _build_system_prompt(monkeypatch, tmp_path: Path, sm: SkillsManager) -> str:
    prompt_file = _make_prompt_yaml(tmp_path)
    monkeypatch.setattr(pb_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_file)
    monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

    result = build_prompt_templates(
        prompt_template_path=None,
        effective_prompt_path=None,
        model_id=None,
        agent_root=tmp_path,
        skills_manager=sm,
        logger=_LOGGER,
    )

    assert result is not None
    return result["system_prompt"]


class TestSkillPromptInjection:
    def test_on_demand_skill_appears_in_catalogue(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "my-normal-skill")

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir))

        system = _build_system_prompt(monkeypatch, tmp_path, sm)

        assert "<available_skills>" in system
        assert "<name>my-normal-skill</name>" in system
        assert "Body of my-normal-skill." not in system
        assert "<eager_loaded_skills>" not in system

    def test_eager_skill_injects_full_body_and_skips_catalogue(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "eager-skill", description="Eager skill")

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir), load_mode="eager")

        system = _build_system_prompt(monkeypatch, tmp_path, sm)

        assert "<eager_loaded_skills>" in system
        assert 'eager_loaded_skill name="eager-skill"' in system
        assert "Body of eager-skill." in system
        assert "<available_skills>" not in system

    def test_mixed_skills_keep_eager_and_on_demand_separate(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "visible-skill", description="I am in catalogue")
        _make_skill(skills_dir, "eager-skill", description="I am injected")

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir / "visible-skill"))
        sm.load_skills_from_directory(str(skills_dir / "eager-skill"), load_mode="eager")

        system = _build_system_prompt(monkeypatch, tmp_path, sm)

        assert "<eager_loaded_skills>" in system
        assert 'eager_loaded_skill name="eager-skill"' in system
        assert "Body of eager-skill." in system
        assert "<available_skills>" in system
        assert "<name>visible-skill</name>" in system
        assert "Body of visible-skill." not in system
        assert system.index("<eager_loaded_skills>") < system.index("<available_skills>")

    def test_legacy_frontmatter_fields_are_ignored_not_mapped(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(
            skills_dir,
            "legacy-fields",
            extra_frontmatter="""
when-to-use: legacy trigger ignored
argument-names: [legacy]
disable-model-invocation: true
user-invocable: false
""",
        )

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir))
        skill = sm.get_skill("legacy-fields")

        assert skill is not None
        assert skill.metadata.when_to_use is None
        assert skill.metadata.arguments is None
        assert not hasattr(skill.metadata, "disable_model_invocation")

        system = _build_system_prompt(monkeypatch, tmp_path, sm)
        assert "<name>legacy-fields</name>" in system
        assert "legacy trigger ignored" not in system
