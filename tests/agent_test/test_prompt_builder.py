"""Unit tests for src.lib.smolagents.prompts.prompt_builder.

These tests verify the prompt resolution chain and assembly logic
independently of BaseAgent.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import src.lib.smolagents.prompts.prompt_builder as pb_module
from src.lib.smolagents.prompts.prompt_builder import (
    build_prompt_templates,
    resolve_model_family_prompt_path,
    resolve_prompt_path,
)
from src.lib.smolagents.skills.catalog import SkillCatalog, SkillSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOGGER = logging.getLogger(__name__)


def _catalog(tmp_path: Path) -> SkillCatalog:
    skill_dir = tmp_path / "skills" / "tagged-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tagged-skill\ndescription: Tagged catalogue entry.\n---\n[PRIVATE_BODY]\n",
        encoding="utf-8",
    )
    return SkillCatalog.discover([SkillSource(tmp_path / "skills", "project")])


# ---------------------------------------------------------------------------
# resolve_model_family_prompt_path
# ---------------------------------------------------------------------------


class TestResolveModelFamilyPromptPath:
    def test_returns_none_for_none(self):
        assert resolve_model_family_prompt_path(None) is None

    def test_returns_none_for_empty_string(self):
        assert resolve_model_family_prompt_path("") is None

    def test_returns_none_when_no_variant_exists(self, tmp_path):
        """When the family directory does not exist, returns None."""
        assert resolve_model_family_prompt_path("nonexistent_vendor/some-model") is None

    def test_returns_path_when_variant_exists(self, monkeypatch, tmp_path):
        family_dir = tmp_path / "myfamily"
        family_dir.mkdir()
        variant = family_dir / "structured_code_agent.yaml"
        variant.write_text("system_prompt: variant", encoding="utf-8")

        monkeypatch.setattr(pb_module, "_PROMPTS_DIR", tmp_path)
        result = resolve_model_family_prompt_path("myfamily/some-model-v2")
        assert result is not None
        assert result == variant.resolve()

    def test_family_is_case_insensitive(self, monkeypatch, tmp_path):
        family_dir = tmp_path / "anthropic"
        family_dir.mkdir()
        variant = family_dir / "structured_code_agent.yaml"
        variant.write_text("system_prompt: anthropic-variant", encoding="utf-8")

        monkeypatch.setattr(pb_module, "_PROMPTS_DIR", tmp_path)
        result = resolve_model_family_prompt_path("Anthropic/aws-claude-sonnet-4-6")
        assert result is not None


# ---------------------------------------------------------------------------
# resolve_prompt_path
# ---------------------------------------------------------------------------


class TestResolvePromptPath:
    def test_explicit_template_path_takes_priority(self, tmp_path):
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text("system_prompt: explicit", encoding="utf-8")

        path, is_explicit = resolve_prompt_path(
            prompt_template_path=str(explicit),
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            logger=_LOGGER,
        )
        assert is_explicit is True
        assert path == explicit.resolve()

    def test_effective_path_used_when_no_explicit(self, tmp_path):
        effective = tmp_path / "effective.yaml"
        effective.write_text("system_prompt: effective", encoding="utf-8")

        path, is_explicit = resolve_prompt_path(
            prompt_template_path=None,
            effective_prompt_path=str(effective),
            model_id=None,
            agent_root=tmp_path,
            logger=_LOGGER,
        )
        assert is_explicit is True
        assert path == effective.resolve()

    def test_model_family_variant_when_no_config(self, monkeypatch, tmp_path):
        family_dir = tmp_path / "testfamily"
        family_dir.mkdir()
        variant = family_dir / "structured_code_agent.yaml"
        variant.write_text("system_prompt: family-variant", encoding="utf-8")
        monkeypatch.setattr(pb_module, "_PROMPTS_DIR", tmp_path)

        path, is_explicit = resolve_prompt_path(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id="testfamily/some-model",
            agent_root=tmp_path,
            logger=_LOGGER,
        )
        assert is_explicit is False
        assert path == variant.resolve()

    def test_falls_back_to_none_when_no_variant(self, monkeypatch, tmp_path):
        """When no explicit path and no model-family variant, returns (None, False)."""
        monkeypatch.setattr(pb_module, "_PROMPTS_DIR", tmp_path)

        path, is_explicit = resolve_prompt_path(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            logger=_LOGGER,
        )
        assert is_explicit is False
        assert path is None


# ---------------------------------------------------------------------------
# build_prompt_templates
# ---------------------------------------------------------------------------


class TestBuildPromptTemplates:
    def test_loads_smolagents_builtin_when_no_explicit_path(self, monkeypatch, tmp_path):
        """When no explicit path is configured, loads smolagents built-in."""
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=SkillCatalog.empty(),
            skill_tool_enabled=False,
            logger=_LOGGER,
        )
        assert isinstance(result, dict)
        assert "system_prompt" in result
        assert "planning" in result

    def test_auto_mode_appends_advisory_todo_policy(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=SkillCatalog.empty(),
            skill_tool_enabled=False,
            logger=_LOGGER,
            tool_call_type="code_act",
            todo_mode="auto",
        )

        assert "decide whether a task list" in result["system_prompt"]
        assert "MUST call `todo_write`" not in result["system_prompt"]
        assert not {"todo_initial", "todo_update", "todo_final"} & set(
            result["planning"]
        )

    def test_on_mode_appends_strong_todo_policy(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=SkillCatalog.empty(),
            skill_tool_enabled=False,
            logger=_LOGGER,
            tool_call_type="tool_call",
            todo_mode="on",
        )

        assert "first tool call MUST be one standalone `todo_write`" in result["system_prompt"]
        assert "read-only discovery" in result["system_prompt"]

    def test_off_mode_omits_todo_policy(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=SkillCatalog.empty(),
            skill_tool_enabled=False,
            logger=_LOGGER,
            todo_mode="off",
        )

        assert "Task Tracking" not in result["system_prompt"]
        assert "todo_write" not in result["system_prompt"]

    def test_appends_environment_prompt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "\n[ENV]")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=SkillCatalog.empty(),
            skill_tool_enabled=False,
            logger=_LOGGER,
        )
        assert "[ENV]" in result["system_prompt"]

    def test_appends_skill_catalogue_without_body(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=_catalog(tmp_path),
            skill_tool_enabled=True,
            logger=_LOGGER,
        )
        system = result["system_prompt"]
        assert "tagged-skill" in system
        assert "Tagged catalogue entry." in system
        assert "[PRIVATE_BODY]" not in system
        assert system.index("tagged-skill") < system.index("## Task Tracking")

    def test_raises_on_missing_explicit_path(self, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            build_prompt_templates(
                prompt_template_path=str(tmp_path / "missing.yaml"),
                effective_prompt_path=None,
                model_id=None,
                agent_root=tmp_path,
                skill_catalog=SkillCatalog.empty(),
                skill_tool_enabled=False,
                logger=_LOGGER,
            )

    def test_returns_none_on_fallback_load_failure(self, monkeypatch, tmp_path):
        """When smolagents built-in fails to load, returns None."""
        # Make the extensions loader raise an error
        def _broken_builtin(tool_call_type, use_structured_output=True):
            raise RuntimeError("simulated failure")
        monkeypatch.setattr(pb_module, "_load_smolagents_builtin", _broken_builtin)

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=SkillCatalog.empty(),
            skill_tool_enabled=False,
            logger=_LOGGER,
        )
        assert result is None

    def test_raises_on_explicit_load_failure(self, tmp_path):
        """When an explicitly configured prompt has bad content, raises ValueError."""
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("not_a_mapping", encoding="utf-8")

        with pytest.raises(ValueError, match="Failed to load"):
            build_prompt_templates(
                prompt_template_path=str(bad_file),
                effective_prompt_path=None,
                model_id=None,
                agent_root=tmp_path,
                skill_catalog=SkillCatalog.empty(),
                skill_tool_enabled=False,
                logger=_LOGGER,
            )

    def test_disabled_skill_tool_hides_nonempty_catalogue(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pb_module, "get_agent_environment_prompt", lambda: "")

        result = build_prompt_templates(
            prompt_template_path=None,
            effective_prompt_path=None,
            model_id=None,
            agent_root=tmp_path,
            skill_catalog=_catalog(tmp_path),
            skill_tool_enabled=False,
            logger=_LOGGER,
        )
        assert "tagged-skill" not in result["system_prompt"]
