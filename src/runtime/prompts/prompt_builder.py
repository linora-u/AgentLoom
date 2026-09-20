"""
Prompt template loading and assembly.

Encapsulates the full prompt resolution chain:
  explicit path → effective agent config → global config → model-family variant → smolagents built-in

And the multi-section assembly:
  base YAML → environment context → available Skill catalogue → mode-aware Todo policy

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
from agentloom.application.validation import resolve_execution_prompt_template_path
from agentloom.runtime.prompts.environment import get_agent_environment_prompt
from agentloom.runtime.skills.catalog import SkillCatalog
from agentloom.runtime.skills.parser import build_skills_prompt

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROMPTS_DIR: Path = Path(__file__).parent.resolve()

DEFAULT_TOOLCALLING_AGENT_PROMPT_PATH: Path = _PROMPTS_DIR / "toolcalling_agent.example.yaml"


def todo_policy_for_mode(mode: str) -> str:
    """Return one provider-independent Todo policy for the configured mode."""

    if mode == "off":
        return ""
    if mode == "auto":
        return """## Task Tracking

For work with three or more meaningful execution steps, multiple deliverables, or substantial uncertainty, decide whether a task list would materially improve execution. If so, call `todo_write` before substantial execution and keep the complete list current. Call `todo_write` alone: never issue it in parallel with another Todo update, another tool, or `final_answer`. Complete the last Todo update before calling `final_answer`. Skip Todo tracking for trivial work, pure questions or answers, and casual conversation. The task list is an aid, not a prerequisite; do not add a separate planning-only turn."""
    if mode == "on":
        return """## Task Tracking

For every non-trivial execution task with multiple steps or deliverables, you MUST establish the task list before substantial execution. When the user input already makes the scope clear, your first tool call MUST be one standalone `todo_write` call. Only when a grounded list cannot yet be written may you first perform the minimum read-only discovery needed to understand scope; call `todo_write` immediately after that discovery and before any mutation or substantive work. Keep exactly one item in progress and update the complete list whenever state changes. Call `todo_write` alone: never issue it in parallel with another Todo update, another tool, or `final_answer`. Complete the last Todo update before calling `final_answer`. Do not create a list for pure questions or answers, one-step work, or casual conversation, even when the reasoning itself has multiple steps."""
    raise ValueError(f"unsupported todo mode: {mode}")


# ---------------------------------------------------------------------------
# Model-family variant resolution
# ---------------------------------------------------------------------------

def resolve_model_family_prompt_path(
    model_id: str | None,
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

    variant_path = (_PROMPTS_DIR / family / "toolcalling_agent.yaml").resolve()
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
    default_prompt_path: Path | None = None,
) -> tuple[Path | None, bool]:
    """Resolve the final prompt YAML path and whether it was explicitly configured.

    Resolution order:
    1. *prompt_template_path* – passed in from execution config
    2. *effective_prompt_path* – from agent / global system config
    3. Model-family variant (e.g. ``anthropic/toolcalling_agent.yaml``)
    4. Local ``toolcalling_agent.yaml`` override in the prompts directory
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
        prompt_path = resolve_execution_prompt_template_path(
            resolved,
            "execution.prompt_template_path",
            agent_root=agent_root,
        )
        return prompt_path, True

    # No explicit config – try model-family variant
    variant_path = resolve_model_family_prompt_path(model_id)
    if variant_path is not None:
        logger.info(
            "Using model-family prompt variant: %s (model_id=%s)",
            variant_path,
            model_id,
        )
        return variant_path, False

    # Try local override: user placed a non-.example file in the prompts dir
    local_override = (_PROMPTS_DIR / "toolcalling_agent.yaml").resolve()
    if local_override.is_file():
        logger.info("Using local prompt override: %s", local_override)
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
        current = templates["system_prompt"]
        separator = "" if current.endswith(("\n", " ")) or section.startswith(("\n", " ")) else "\n\n"
        templates["system_prompt"] = current + separator + section


def _load_smolagents_builtin() -> dict[str, Any]:
    """Load smolagents' built-in prompt template from the installed package."""
    content = (
        importlib.resources.files("smolagents.prompts")
        .joinpath("toolcalling_agent.yaml")
        .read_text()
    )
    return yaml.safe_load(content)


def load_base_prompt_templates(
    *,
    prompt_template_path: str | None,
    effective_prompt_path: str | None = None,
    model_id: str | None,
    agent_root: Path | str,
    logger: Any,
) -> dict[str, Any] | None:
    """Resolve and load native prompt templates without injecting runtime context.

    Explicitly configured paths fail closed. Model-family and local overrides
    retain the existing resolution order; a failure on the implicit built-in
    chain returns ``None`` so the concrete runtime may use its native
    instructions fallback.
    """

    prompt_path, explicit_configured = resolve_prompt_path(
        prompt_template_path=prompt_template_path,
        effective_prompt_path=effective_prompt_path,
        model_id=model_id,
        agent_root=agent_root,
        logger=logger,
    )

    if explicit_configured and prompt_path is not None and (
        not prompt_path.exists() or not prompt_path.is_file()
    ):
        raise ValueError(
            f"Configured prompt path does not exist or is not a file: {prompt_path}"
        )

    try:
        if prompt_path is not None:
            return _load_and_validate_yaml(prompt_path)
        return _load_smolagents_builtin()
    except Exception as exc:
        if explicit_configured:
            raise ValueError(
                f"Failed to load configured prompt template '{prompt_path}': {exc}"
            ) from exc
        logger.warning("Failed to load base prompt templates: %s", exc)
        return None


def build_prompt_templates(
    *,
    prompt_template_path: str | None,
    effective_prompt_path: str | None,
    model_id: str | None,
    agent_root: Path | str,
    skill_catalog: SkillCatalog,
    skill_tool_enabled: bool,
    logger: Any,
    todo_mode: str = "auto",
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
    prompt_templates = load_base_prompt_templates(
        prompt_template_path=prompt_template_path,
        effective_prompt_path=effective_prompt_path,
        model_id=model_id,
        agent_root=agent_root,
        logger=logger,
    )
    if prompt_templates is None:
        return None

    # 1) Environment context (workspace root, exclusions)
    _append_to_system_prompt(prompt_templates, get_agent_environment_prompt())

    # 2) Advertise the same resolved catalogue used by the skill tool.
    if skill_tool_enabled:
        _append_to_system_prompt(
            prompt_templates,
            build_skills_prompt(skill_catalog.summaries()),
        )

    # 3) Keep the mode policy last so long environment/skill sections do
    # not bury the current task-tracking contract.
    todo_policy = todo_policy_for_mode(todo_mode)
    if todo_policy:
        _append_to_system_prompt(prompt_templates, todo_policy)

    return prompt_templates
