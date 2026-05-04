import json

from src.trace.task_context import get_current_skills_manager
from src.lib.logging import get_logger
from src.lib.smolagents.skills.skills import SkillsManager

logger = get_logger(__name__)


def _resolve_skills_manager() -> SkillsManager:
    current_manager = get_current_skills_manager()
    if current_manager is not None:
        return current_manager
    return SkillsManager.get_instance()


def load_skill(skill: str, args: str = "") -> str:
    """
    load a skill's instructions by name.

    Use this immediately when a skill applies. The returned text contains the
    full instructions you must follow to complete the task.

    Args:
        skill: Skill identifier, e.g. "commit", "review-pr", "pdf".
        args: Arguments passed through for context (empty string = no args).

    Returns:
        A formatted string containing:
        - Skill name
        - Optional description
        - Optional provided arguments
        - Full skill instructions

    Errors:
        ValueError if `skill` is missing or unknown. The error message lists available skills.

    Usage:
        load_skill("commit", "-m 'Fix authentication bug'")
        load_skill("review-pr", "123")
        load_skill("pdf")
    """
    if not skill or not isinstance(skill, str):
        raise ValueError("skill is required and must be a non-empty string")

    skills_manager = _resolve_skills_manager()
    skill_content = skills_manager.get_skill_content(skill)
    if not skill_content or skill_content.metadata.invocation_control.get('allow-model', True) is False:
        available = sorted([
            name for name, s in skills_manager.skills.items()
            if s.metadata.invocation_control.get("allow-model", True) is not False
        ])
        available_text = ", ".join(available) if available else "(none)"
        raise ValueError(f"Skill '{skill}' not found. Available skills: {available_text}")

    # Deduplication: if this skill has allow-model: "force-inject", its instructions
    # are already in the system prompt — return a short notice to save tokens.
    if skill_content.metadata.invocation_control.get('allow-model') == "force-inject":
        logger.info("load_skill called for force-injected skill '%s' — returning dedup notice", skill)
        return (
            f"<skill_already_loaded>\n"
            f"Skill '{skill}' has already been force-injected into the system prompt.\n"
            f"Its full instructions are already in your context under <force_injected_skills>.\n"
            f"You do NOT need to call load_skill for this skill. "
            f"Proceed to follow the instructions already present in your system prompt.\n"
            f"</skill_already_loaded>"
        )

    allowed_tools = skill_content.metadata.allowed_tools

    result_lines = [f"<skill_name>{skill}</skill_name>"]
    description = skill_content.metadata.description
    if description:
        result_lines.append(f"<description>{description}</description>")
    
    if allowed_tools:
        result_lines.append("<allowed_tools>")
        result_lines.append("  <!-- You must ONLY use the tools listed below for this skill -->")
        for tool in allowed_tools:
            result_lines.append(f"  <tool>{tool}</tool>")
        result_lines.append("</allowed_tools>")

    if args:
        result_lines.append(f"<arguments>{args}</arguments>")

    result_lines.append("<instructions>")
    result_lines.append(skill_content.instructions)
    result_lines.append("</instructions>")
    result = "\n".join(result_lines)
    logger.info("Skill tool result returned: %s", skill)
    return result


def list_skills(include_description: bool = True) -> str:
    """
    AI TOOL — list available skills as JSON.

    Args:
        include_description: If True, include each skill's description.

    Returns:
        JSON string, e.g.:
            [{"name": "commit", "description": "Commit workflow"}, ...]

    Usage:
        list_skills()
        list_skills(include_description=False)
    """
    skills_manager = _resolve_skills_manager()
    skills = []
    for skill in sorted(skills_manager.skills.values(), key=lambda s: s.metadata.name):
        if skill.metadata.invocation_control.get('allow-model', True) is False:
            continue
        item = {"name": skill.metadata.name}
        if include_description:
            item["description"] = skill.metadata.description
        skills.append(item)
    return json.dumps(skills, ensure_ascii=False)
