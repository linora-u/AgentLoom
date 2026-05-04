"""
Prompt template loading and assembly.

Encapsulates the full prompt resolution chain:
  explicit path → effective agent config → global config → model-family variant → default

And the multi-section assembly:
  base YAML → environment context → force-injected skills → on-demand skills catalogue

This module is intentionally *stateless* – every public entry point receives
all required data via arguments so that it can be unit-tested without
instantiating a full :class:`BaseAgent`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from src.lib.smolagents.agent.agent_env import get_agent_environment_prompt
from src.lib.smolagents.agent.agent_validation import resolve_execution_prompt_template_path
from src.lib.smolagents.skills.skills import SkillsManager


# ---------------------------------------------------------------------------
# Default paths – mirrors the constants previously in base_agent.py
# ---------------------------------------------------------------------------

DEFAULT_CODE_AGENT_PROMPT_PATH: Path = (
    Path(__file__).parent / "code_agent.yaml"
).resolve()

DEFAULT_TOOLCALLING_AGENT_PROMPT_PATH: Path = (
    Path(__file__).parent / "toolcalling_agent.yaml"
).resolve()

_PROMPTS_DIR: Path = DEFAULT_CODE_AGENT_PROMPT_PATH.parent

def get_prompt_filename_for_tool_call_type(tool_call_type: str) -> str:
    """Return the base prompt filename based on tool_call_type.
    
    Args:
        tool_call_type: Either "tool_call" or "code_act"
    
    Returns:
        "toolcalling_agent.yaml" for "tool_call", "code_agent.yaml" otherwise
    """
    if tool_call_type == "tool_call":
        return "toolcalling_agent.yaml"
    return "code_agent.yaml"


# ---------------------------------------------------------------------------
# Model-family variant resolution
# ---------------------------------------------------------------------------

def resolve_model_family_prompt_path(
    model_id: str | None,
    tool_call_type: str = "code_act"
) -> Path | None:
    """Try to find a model-family-specific prompt variant.

    Given a *model_id* like ``"anthropic/aws-claude-sonnet-4-6"`` the function
    extracts the first path segment (``"anthropic"``) and checks whether
    ``<prompts_dir>/<family>/<prompt_filename>`` exists.

    Returns the resolved :class:`Path` when a variant is found, otherwise
    ``None`` so that callers can fall back to the default prompt.
    """
    if not model_id:
        return None
    family = model_id.split("/")[0].lower().strip()
    if not family:
        return None
        
    prompt_filename = get_prompt_filename_for_tool_call_type(tool_call_type)
    variant_path = (_PROMPTS_DIR / family / prompt_filename).resolve()
    if variant_path.is_file():
        return variant_path
    return None


# ---------------------------------------------------------------------------
# Prompt path resolution chain
# ---------------------------------------------------------------------------

def resolve_prompt_path(
    *,
    prompt_template_path: str | None,
    effective_prompt_path: str | None,
    model_id: str | None,
    agent_root: Path | str,
    logger: Any,
    tool_call_type: str = "code_act",
    default_prompt_path: Path | None = None,
) -> tuple[Path, bool]:
    """Resolve the final prompt YAML path and whether it was explicitly configured.

    Resolution order:
    1. *prompt_template_path* – passed in from execution config
    2. *effective_prompt_path* – from agent / global system config
    3. Model-family variant (e.g. ``anthropic/code_agent.yaml``)
    4. Default prompt path (fallback)

    Returns:
        ``(resolved_path, explicit_configured)`` – *explicit_configured* is
        ``True`` when the path originated from an explicit configuration value
        (cases 1 & 2).
    """
    resolved = prompt_template_path
    if resolved is None:
        resolved = effective_prompt_path

    if resolved is not None:
        code_agent_prompt_path = resolve_execution_prompt_template_path(
            resolved,
            "execution.prompt_template_path",
            agent_root=agent_root,
        )
        return code_agent_prompt_path, True

    # No explicit config – try model-family variant
    variant_path = resolve_model_family_prompt_path(model_id, tool_call_type)
    if variant_path is not None:
        logger.info(
            "Using model-family prompt variant: %s (model_id=%s, tool_call_type=%s)",
            variant_path,
            model_id,
            tool_call_type,
        )
        return variant_path, False

    # Fall back to provided default, or standard default
    final_default = default_prompt_path or DEFAULT_CODE_AGENT_PROMPT_PATH
    return final_default, False


# ---------------------------------------------------------------------------
# YAML loading + multi-section assembly
# ---------------------------------------------------------------------------

def _load_and_validate_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and verify it contains a mapping."""
    prompt_templates = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(prompt_templates, dict):
        raise ValueError(f"Prompt template file must contain a YAML mapping: {path}")
    return prompt_templates


def _append_to_system_prompt(templates: dict[str, Any], section: str) -> None:
    """Append *section* to the ``system_prompt`` key if both are non-empty."""
    if section and "system_prompt" in templates:
        templates["system_prompt"] += section


def build_prompt_templates(
    *,
    prompt_template_path: str | None,
    effective_prompt_path: str | None,
    model_id: str | None,
    agent_root: Path | str,
    skills_manager: SkillsManager | None,
    logger: Any,
    tool_call_type: str = "code_act",
) -> dict[str, Any] | None:
    """Build the final prompt-templates dict ready for the runtime agent.

    This is the single entry-point that replaces
    :pymethod:`BaseAgent._build_prompt_templates`.  It is a pure function:
    all dependencies are injected via keyword arguments.

    Returns ``None`` when prompt loading fails on a non-explicit (fallback) path
    so that the caller can gracefully degrade.

    Raises :class:`ValueError` when an *explicitly configured* prompt path is
    missing or cannot be loaded.
    """
    default_prompt_path = (
        DEFAULT_TOOLCALLING_AGENT_PROMPT_PATH
        if tool_call_type == "tool_call"
        else DEFAULT_CODE_AGENT_PROMPT_PATH
    )

    code_agent_prompt_path, explicit_configured = resolve_prompt_path(
        prompt_template_path=prompt_template_path,
        effective_prompt_path=effective_prompt_path,
        model_id=model_id,
        agent_root=agent_root,
        logger=logger,
        tool_call_type=tool_call_type,
        default_prompt_path=default_prompt_path,
    )

    if explicit_configured and (
        not code_agent_prompt_path.exists() or not code_agent_prompt_path.is_file()
    ):
        raise ValueError(
            f"Configured prompt path does not exist or is not a file: {code_agent_prompt_path}"
        )

    try:
        prompt_templates = _load_and_validate_yaml(code_agent_prompt_path)

        # 1) Environment context (workspace root, exclusions)
        _append_to_system_prompt(prompt_templates, get_agent_environment_prompt())

        # 2) Resolve skills manager
        resolved_skills = skills_manager
        if resolved_skills is None:
            resolved_skills = SkillsManager.get_instance(logger=logger)

        # 3) Force-injected skills (full instructions)
        _append_to_system_prompt(
            prompt_templates, resolved_skills.get_force_injected_prompt()
        )

        # 4) On-demand skills catalogue
        _append_to_system_prompt(
            prompt_templates, resolved_skills.get_skills_prompt()
        )

        return prompt_templates

    except Exception as exc:
        if explicit_configured:
            raise ValueError(
                f"Failed to load configured prompt template '{code_agent_prompt_path}': {exc}"
            ) from exc
        logger.warning("Failed to load or patch customized prompt: %s", exc)
        return None
