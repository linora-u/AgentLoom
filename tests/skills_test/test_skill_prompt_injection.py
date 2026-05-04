"""End-to-end tests verifying that skills are correctly injected into (or
excluded from) the agent system prompt via ``build_prompt_templates()``.

Each test creates real SKILL.md files on disk and a minimal prompt YAML,
then calls the full prompt assembly pipeline to confirm presence / absence
of specific skill names in the final ``system_prompt``.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_LOOM_ROOT = SCRIPT_DIR.parents[1]
if str(AGENT_LOOM_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_LOOM_ROOT))

import src.lib.smolagents.prompts.prompt_builder as pb_module
from src.lib.smolagents.prompts.prompt_builder import build_prompt_templates
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.skills.skills import SkillsManager

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prompt_yaml(tmp_path: Path) -> Path:
    """Create a minimal ``code_agent.yaml`` used by ``build_prompt_templates``."""
    prompt_file = tmp_path / "code_agent.yaml"
    prompt_file.write_text("system_prompt: base", encoding="utf-8")
    return prompt_file


def _make_skill(
    parent_dir: Path,
    name: str,
    description: str = "A test skill",
) -> Path:
    """Create a SKILL.md inside ``<parent_dir>/<name>/SKILL.md``."""
    skill_dir = parent_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        "---",
        f"# {name} instructions",
        f"Body of {name}.",
        "",
    ]
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("\n".join(lines), encoding="utf-8")
    return skill_path


def _fresh_skills_manager() -> SkillsManager:
    """Return a brand-new SkillsManager that does NOT share global state."""
    SkillsManager._instance = None
    HookManager._instance = None
    return SkillsManager(
        logger=logging.getLogger(__name__),
        hook_manager=HookManager(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSkillPromptInjection:
    """Verify ``allow_model`` controls prompt catalogue inclusion."""

    def test_normal_skill_appears_in_prompt(self, monkeypatch, tmp_path):
        """A skill with default settings (no allow_model set, defaults to True) shows
        up in the ``<available_skills>`` catalogue inside the system prompt."""
        prompt_file = _make_prompt_yaml(tmp_path)
        monkeypatch.setattr(pb_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_file)
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "my-normal-skill")

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir))

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skills_manager=sm,
            logger=_LOGGER,
        )

        assert result is not None
        system = result["system_prompt"]
        assert "<available_skills>" in system
        assert "<name>my-normal-skill</name>" in system

    def test_disabled_skill_excluded_from_prompt(self, monkeypatch, tmp_path):
        """A skill with ``allow-model: false`` must NOT appear
        in the system prompt catalogue at all."""
        prompt_file = _make_prompt_yaml(tmp_path)
        monkeypatch.setattr(pb_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_file)
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "hidden-skill")

        ic_hidden = {"allow-model": False, "allow-hook": True}
        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir), invocation_control=ic_hidden)

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skills_manager=sm,
            logger=_LOGGER,
        )

        assert result is not None
        system = result["system_prompt"]
        # The disabled skill must be completely absent from the prompt
        assert "<name>hidden-skill</name>" not in system
        # With only disabled skills, no catalogue section should exist
        assert "<available_skills>" not in system

    def test_mixed_skills_only_enabled_in_prompt(self, monkeypatch, tmp_path):
        """When both enabled and disabled skills exist, only enabled ones
        appear in the prompt catalogue."""
        prompt_file = _make_prompt_yaml(tmp_path)
        monkeypatch.setattr(pb_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_file)
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "visible-skill", description="I am visible")
        _make_skill(skills_dir, "invisible-skill", description="I am hidden")

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir / "visible-skill"))
        ic_hidden = {"allow-model": False, "allow-hook": True}
        sm.load_skills_from_directory(str(skills_dir / "invisible-skill"), invocation_control=ic_hidden)

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skills_manager=sm,
            logger=_LOGGER,
        )

        assert result is not None
        system = result["system_prompt"]
        assert "<available_skills>" in system
        assert "<name>visible-skill</name>" in system
        assert "<description>I am visible</description>" in system
        # The disabled skill must NOT be present
        assert "<name>invisible-skill</name>" not in system
        assert "I am hidden" not in system

    def test_force_inject_skill_in_prompt_regardless(self, monkeypatch, tmp_path):
        """A skill with ``allow-model: "force-inject"`` appears in the system
        prompt's force-injected section and is excluded from the catalogue."""
        prompt_file = _make_prompt_yaml(tmp_path)
        monkeypatch.setattr(pb_module, "DEFAULT_CODE_AGENT_PROMPT_PATH", prompt_file)
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "injected-skill", description="Force injected")

        ic_inject = {"allow-model": "force-inject", "allow-hook": True}
        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir), invocation_control=ic_inject)

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skills_manager=sm,
            logger=_LOGGER,
        )

        assert result is not None
        system = result["system_prompt"]
        assert "<force_injected_skills>" in system
        assert 'force_injected_skill name="injected-skill"' in system
        assert "Force injected" in system
        # Force-injected skills should NOT also appear in the on-demand catalogue
        assert "<name>injected-skill</name>" not in system

    def test_allow_model_default_is_true(self, tmp_path):
        """When invocation_control is not passed, defaults to True."""
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "default-skill")

        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir))

        skill = sm.get_skill("default-skill")
        assert skill is not None
        assert skill.metadata.invocation_control.get('allow-model') is True

    def test_allow_model_false_via_parameter(self, tmp_path):
        """``allow-model: false`` passed via parameter is applied correctly."""
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "disabled-skill")

        ic_hidden = {"allow-model": False, "allow-hook": True}
        sm = _fresh_skills_manager()
        sm.load_skills_from_directory(str(skills_dir), invocation_control=ic_hidden)

        skill = sm.get_skill("disabled-skill")
        assert skill is not None
        assert skill.metadata.invocation_control.get('allow-model') is False
