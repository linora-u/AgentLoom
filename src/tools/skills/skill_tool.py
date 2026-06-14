import json
from pathlib import Path

from src.trace.task_context import get_current_skills_manager
from src.lib.logging import get_logger
from src.lib.smolagents.skills.skills import (
    SKILL_INLINE_MAX_CHARS,
    SKILL_INLINE_PREVIEW_LINES,
    SkillsManager,
)

logger = get_logger(__name__)


def _resolve_skills_manager() -> SkillsManager:
    current_manager = get_current_skills_manager()
    if current_manager is not None:
        return current_manager
    return SkillsManager.get_instance()


def _available_skill_names(skills_manager: SkillsManager) -> list[str]:
    return sorted(skills_manager.skills)


def _append_resource_index(result_lines: list[str], skills_manager: SkillsManager, skill: str) -> None:
    resources = skills_manager.list_skill_resources(skill)
    if not resources:
        return
    result_lines.append("<skill_resources>")
    result_lines.append("  <!-- Read bundled files with read_skill_resource(skill, path, offset, limit). -->")
    for resource in resources[:80]:
        result_lines.append(
            "  "
            f"<resource path=\"{resource['path']}\" "
            f"kind=\"{resource['kind']}\" bytes=\"{resource['bytes']}\" />"
        )
    if len(resources) > 80:
        result_lines.append(f"  <!-- {len(resources) - 80} more resources omitted from listing -->")
    result_lines.append("</skill_resources>")


def load_skill(skill: str, args: str = "") -> str:
    """
    Load a skill's instructions by name.

    Use this immediately when an on-demand skill applies. Eager skills are
    already present in the system prompt and return a short deduplication note.

    Args:
        skill: Name of the configured skill to load.
        args: Optional argument string to pass through to the skill instructions.
    """
    if not skill or not isinstance(skill, str):
        raise ValueError("skill is required and must be a non-empty string")

    skills_manager = _resolve_skills_manager()
    skill_obj = skills_manager.get_skill(skill)
    if skill_obj is None:
        available = _available_skill_names(skills_manager)
        available_text = ", ".join(available) if available else "(none)"
        raise ValueError(f"Skill '{skill}' not found. Available skills: {available_text}")

    if skill_obj.metadata.load_mode == "eager":
        result_lines = [
            "<skill_already_loaded>",
            f"Skill '{skill}' has already been eagerly loaded into the system prompt.",
            "Its full instructions are already in your context under <eager_loaded_skills>.",
            "Do not call load_skill again for this skill; follow the instructions already present.",
            "</skill_already_loaded>",
        ]
        _append_resource_index(result_lines, skills_manager, skill)
        return "\n".join(result_lines)

    skill_content = skills_manager.get_skill_content(skill)
    if not skill_content:
        available = _available_skill_names(skills_manager)
        available_text = ", ".join(available) if available else "(none)"
        raise ValueError(f"Skill '{skill}' not found. Available skills: {available_text}")

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

    _append_resource_index(result_lines, skills_manager, skill)

    instructions = skill_content.instructions
    if len(instructions) <= SKILL_INLINE_MAX_CHARS:
        result_lines.append("<instructions>")
        result_lines.append(instructions)
        result_lines.append("</instructions>")
    else:
        lines = instructions.splitlines()
        preview = "\n".join(lines[:SKILL_INLINE_PREVIEW_LINES])
        result_lines.append(
            f"<instructions budgeted=\"true\" "
            f"total_chars=\"{len(instructions)}\" total_lines=\"{len(lines)}\" "
            f"included_lines=\"1-{min(SKILL_INLINE_PREVIEW_LINES, len(lines))}\">"
        )
        result_lines.append(preview)
        result_lines.append("</instructions>")
        result_lines.append("<budgeted_loading_guidance>")
        result_lines.append(
            "The skill is large, so only the opening section was returned inline. "
            "Use read_skill_resource with path='SKILL.md' or another listed resource "
            "to read the exact lines needed for the current task."
        )
        result_lines.append("</budgeted_loading_guidance>")

    result = "\n".join(result_lines)
    logger.info("Skill tool result returned: %s", skill)
    return result


def list_skills(include_description: bool = True, detail: str = "summary") -> str:
    """
    AI TOOL — list available skills as JSON.

    Args:
        include_description: If True, include each skill's description.
        detail: "summary" for name/description, "full" for path and runtime policy.
    """
    skills_manager = _resolve_skills_manager()
    skills = []
    for skill in sorted(skills_manager.skills.values(), key=lambda s: s.metadata.name):
        item = {"name": skill.metadata.name}
        if include_description:
            item["description"] = skill.metadata.description
        if detail == "full":
            item.update(
                {
                    "file_path": skill.file_path,
                    "base_dir": str(Path(skill.file_path).parent),
                    "platform": skill.metadata.platform,
                    "allowed_tools": skill.metadata.allowed_tools,
                    "argument_hint": skill.metadata.argument_hint,
                    "arguments": skill.metadata.arguments,
                    "when_to_use": skill.metadata.when_to_use,
                    "context": skill.metadata.context,
                    "agent": skill.metadata.agent,
                    "effort": skill.metadata.effort,
                    "load_mode": skill.metadata.load_mode,
                    "allow_scripts": skill.metadata.allow_scripts,
                    "allow_network": skill.metadata.allow_network,
                }
            )
        skills.append(item)
    return json.dumps(skills, ensure_ascii=False)


def read_skill_resource(skill: str, path: str, offset: int = 1, limit: int = 200) -> str:
    """
    Read a bundled file from a loaded skill package.

    Args:
        skill: Name of the configured skill that owns the resource.
        path: Resource path relative to the skill directory.
        offset: One-based first line to read.
        limit: Maximum number of lines to return.
    """
    skills_manager = _resolve_skills_manager()
    data = skills_manager.read_skill_resource(skill, path, offset=offset, limit=limit)
    return json.dumps(data, ensure_ascii=False)


def check_skill_dependencies(skill: str) -> str:
    """
    Check discoverable dependencies for a skill package.

    Args:
        skill: Name of the configured skill to inspect.
    """
    skills_manager = _resolve_skills_manager()
    data = skills_manager.check_skill_dependencies(skill)
    return json.dumps(data, ensure_ascii=False)


def run_skill_script(
    skill: str,
    command: str,
    args: str = "",
    cwd: str = "skill",
    timeout: int = 60,
    env_allowlist: str = "",
    allow_network: bool = True,
) -> str:
    """
    Execute a script or command for a skill package with an audit trail.

    Args:
        skill: Name of the configured skill that owns the script.
        command: Shell command to execute.
        args: Optional argument string appended to the command.
        cwd: Working directory mode, either "skill" or "workspace".
        timeout: Maximum execution time in seconds.
        env_allowlist: Optional comma or whitespace separated list of inherited environment names.
        allow_network: Whether this invocation permits common network commands.
    """
    skills_manager = _resolve_skills_manager()
    data = skills_manager.run_skill_script(
        skill,
        command,
        args=args,
        cwd=cwd,
        timeout=timeout,
        env_allowlist=env_allowlist,
        allow_network=allow_network,
    )
    return json.dumps(data, ensure_ascii=False)
