"""
Prompt template loading and assembly.

Encapsulates the full prompt resolution chain:
  explicit path → effective agent config → global config → model-family variant → smolagents built-in

And the multi-section assembly:
  base YAML → todo section injection → environment context →
  eager skills → on-demand skills catalogue

When no explicit prompt path is configured, the module uses smolagents' native
built-in prompt template. Users can provide custom prompt YAML files (see
*.example.yaml for reference templates) via ``prompt_template_path`` config.

This module is intentionally *stateless* – every public entry point receives
all required data via arguments so that it can be unit-tested without
instantiating a full :class:`BaseAgent`.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any

import yaml

from src.lib.smolagents.agent.agent_env import get_agent_environment_prompt
from src.lib.smolagents.agent.agent_validation import resolve_execution_prompt_template_path
from src.lib.smolagents.skills.skills import SkillsManager


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROMPTS_DIR: Path = Path(__file__).parent.resolve()

# Legacy constants kept for backward compatibility
DEFAULT_CODE_AGENT_PROMPT_PATH: Path = _PROMPTS_DIR / "structured_code_agent.example.yaml"
DEFAULT_TOOLCALLING_AGENT_PROMPT_PATH: Path = _PROMPTS_DIR / "toolcalling_agent.example.yaml"

def get_prompt_filename_for_tool_call_type(tool_call_type: str, use_structured_output: bool = True) -> str:
    """Return the base prompt filename based on tool_call_type.
    
    Args:
        tool_call_type: Either "tool_call" or "code_act"
        use_structured_output: Whether to use structured output (json_schema) for code_act
    
    Returns:
        "toolcalling_agent.yaml" for "tool_call",
        "structured_code_agent.yaml" for code_act with structured output,
        "code_agent.yaml" for code_act without structured output.
    """
    if tool_call_type == "tool_call":
        return "toolcalling_agent.yaml"
    if use_structured_output:
        return "structured_code_agent.yaml"
    return "code_agent.yaml"


# ---------------------------------------------------------------------------
# Model-family variant resolution
# ---------------------------------------------------------------------------

def resolve_model_family_prompt_path(
    model_id: str | None,
    tool_call_type: str = "code_act",
    use_structured_output: bool = True,
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
        
    prompt_filename = get_prompt_filename_for_tool_call_type(tool_call_type, use_structured_output)
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
    use_structured_output: bool = True,
) -> tuple[Path | None, bool]:
    """Resolve the final prompt YAML path and whether it was explicitly configured.

    Resolution order:
    1. *prompt_template_path* – passed in from execution config
    2. *effective_prompt_path* – from agent / global system config
    3. Model-family variant (e.g. ``anthropic/toolcalling_agent.yaml``)
    4. Local override in prompts dir (e.g. ``structured_code_agent.yaml`` without .example)
    5. None (use smolagents' built-in)

    Returns:
        ``(resolved_path, explicit_configured)`` – *explicit_configured* is
        ``True`` when the path originated from an explicit configuration value
        (cases 1 & 2). Returns ``(None, False)`` when no explicit path and no
        override found (caller should use smolagents built-in).
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
    variant_path = resolve_model_family_prompt_path(model_id, tool_call_type, use_structured_output)
    if variant_path is not None:
        logger.info(
            "Using model-family prompt variant: %s (model_id=%s, tool_call_type=%s)",
            variant_path,
            model_id,
            tool_call_type,
        )
        return variant_path, False

    # Try local override: user placed a non-.example file in the prompts dir
    prompt_filename = get_prompt_filename_for_tool_call_type(tool_call_type, use_structured_output)
    local_override = (_PROMPTS_DIR / prompt_filename).resolve()
    if local_override.is_file():
        logger.info(
            "Using local prompt override: %s (tool_call_type=%s)",
            local_override,
            tool_call_type,
        )
        return local_override, False

    # No override found – use smolagents' built-in
    return None, False


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


def _load_smolagents_builtin(tool_call_type: str, use_structured_output: bool = True) -> dict[str, Any]:
    """Load smolagents' built-in prompt template from the installed package."""
    if tool_call_type == "tool_call":
        filename = "toolcalling_agent.yaml"
    elif use_structured_output:
        filename = "structured_code_agent.yaml"
    else:
        filename = "code_agent.yaml"
    content = importlib.resources.files("smolagents.prompts").joinpath(filename).read_text()
    return yaml.safe_load(content)


def build_prompt_templates(
    *,
    prompt_template_path: str | None,
    effective_prompt_path: str | None,
    model_id: str | None,
    agent_root: Path | str,
    skills_manager: SkillsManager | None,
    logger: Any,
    tool_call_type: str = "code_act",
    use_structured_output: bool = True,
) -> dict[str, Any] | None:
    """Build the final prompt-templates dict ready for the runtime agent.

    Resolution logic:
    - If user provides an explicit prompt path (via YAML config), load that file.
    - If a model-family variant exists, use it.
    - Otherwise, use smolagents' built-in prompt template.

    In all cases, appends environment context and skills to system_prompt.

    Returns ``None`` when prompt loading fails on a non-explicit (fallback) path
    so that the caller can gracefully degrade.

    Raises :class:`ValueError` when an *explicitly configured* prompt path is
    missing or cannot be loaded.
    """
    code_agent_prompt_path, explicit_configured = resolve_prompt_path(
        prompt_template_path=prompt_template_path,
        effective_prompt_path=effective_prompt_path,
        model_id=model_id,
        agent_root=agent_root,
        logger=logger,
        tool_call_type=tool_call_type,
        use_structured_output=use_structured_output,
    )

    if explicit_configured and code_agent_prompt_path is not None and (
        not code_agent_prompt_path.exists() or not code_agent_prompt_path.is_file()
    ):
        raise ValueError(
            f"Configured prompt path does not exist or is not a file: {code_agent_prompt_path}"
        )

    try:
        if code_agent_prompt_path is not None:
            # User-provided path or model-family variant
            prompt_templates = _load_and_validate_yaml(code_agent_prompt_path)
        else:
            # Default: use smolagents' built-in prompt
            prompt_templates = _load_smolagents_builtin(tool_call_type, use_structured_output)

        # 1) Environment context (workspace root, exclusions)
        _append_to_system_prompt(prompt_templates, get_agent_environment_prompt())

        # 2) Resolve skills manager
        resolved_skills = skills_manager
        if resolved_skills is None:
            resolved_skills = SkillsManager.get_instance(logger=logger)

        # 3) Eager skills (full instructions)
        _append_to_system_prompt(
            prompt_templates, resolved_skills.get_eager_skills_prompt()
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
