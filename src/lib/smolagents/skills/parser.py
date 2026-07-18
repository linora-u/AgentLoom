"""Skill file parsing and prompt generation.

AgentLoom skills use Claude Code style ``SKILL.md`` packages.  The parser is
strict about the required contract (valid YAML frontmatter with ``name`` and
``description``) and intentionally ignores unknown frontmatter fields instead
of mapping legacy aliases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

from src.lib.logging import get_logger

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_NAME_LENGTH = 64
_MAX_SKILL_DESCRIPTION_LENGTH = 1024


@dataclass
class SkillMetadata:
    name: str
    description: str
    version: str | None = None
    allowed_tools: list[str] | None = None
    platform: str | None = None
    argument_hint: str | None = None
    arguments: list[str] | None = None
    when_to_use: str | None = None
    model: str | None = None
    context: str | None = None
    agent: str | None = None
    effort: str | None = None
    load_mode: str = "on-demand"
    allow_scripts: bool = True
    allow_network: bool = True
    policy_priority: int = -1


@dataclass
class Skill:
    metadata: SkillMetadata
    content: str | None
    file_path: str

    @property
    def base_dir(self) -> str:
        return str(Path(self.file_path).parent)


@dataclass
class SkillContent:
    metadata: SkillMetadata
    instructions: str


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

SKILLS_PROMPT = """
====

AVAILABLE SKILLS

<available_skills>
{skillsXml}
</available_skills>

<mandatory_skill_check>
REQUIRED PRECONDITION

Before producing ANY user-facing response, you MUST perform a skill applicability check.

Step 1: Skill Evaluation
- Evaluate the user's request against ALL available skill <description> entries in <available_skills>.
- Determine whether at least one skill clearly and unambiguously applies.

Step 2: Branching Decision

<if_skill_applies>
- Select EXACTLY ONE skill.
- Prefer the most specific skill when multiple skills match.
- Use the load_skill tool to load the skill by name.
- Load the skill's instructions fully into context BEFORE continuing.
- Follow the skill instructions precisely.
- Do NOT respond outside the skill-defined flow.
</if_skill_applies>

<if_no_skill_applies>
- Proceed with a normal response.
- Do NOT load any SKILL.md files.
</if_no_skill_applies>

CONSTRAINTS:
- Do NOT load every skill up front.
- Load skills ONLY after a skill is selected.
- Do NOT skip this check.
- FAILURE to perform this check is an error.
</mandatory_skill_check>

<linked_file_handling>
- When a skill is loaded, ONLY the skill instructions are present.
- Files linked from the skill are NOT loaded automatically.
- The model MUST explicitly decide to read a linked file based on task relevance.
- Do NOT assume the contents of linked files unless they have been explicitly read.
- Prefer reading the minimum necessary linked file.
- Avoid reading multiple linked files unless required.
- Treat linked files as progressive disclosure, not mandatory context.
</linked_file_handling>

<internal_verification>
This section is for internal control only.
Do NOT include this section in user-facing output.

After completing the evaluation, internally confirm:
<skill_check_completed>true|false</skill_check_completed>
</internal_verification>
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_skill_file(file_path: str, logger=None) -> tuple[SkillMetadata, str]:
    """Parse a skill file and return ``(SkillMetadata, markdown_body)``."""
    logger = get_logger(logger, __name__)

    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    try:
        post = frontmatter.loads(content)
        data = post.metadata
        markdown_body = post.content
        if content.endswith(("\n", "\r\n")) and not markdown_body.endswith(("\n", "\r\n")):
            markdown_body += "\n"
    except Exception as e:
        raise ValueError(f"Invalid skill frontmatter in {file_path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Skill frontmatter must be a YAML mapping: {file_path}")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Skill frontmatter requires non-empty string field 'name': {file_path}")
    name = name.strip()
    _validate_skill_name(name, file_path)

    description = data.get("description", "")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Skill frontmatter requires non-empty string field 'description': {file_path}")
    description = description.strip()
    if len(description) > _MAX_SKILL_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Skill description exceeds {_MAX_SKILL_DESCRIPTION_LENGTH} characters: {file_path}"
        )

    version = data.get("version")
    if not isinstance(version, str):
        version = None

    allowed_tools = _parse_string_list(data.get("allowed-tools"), field_name="allowed-tools")

    if "hooks" in data:
        raise ValueError(
            "SKILL.md field 'hooks' is not supported; configure a direct Hook "
            f"or standalone Hook Bundle instead: {file_path}"
        )
    if "enable-hooks" in data:
        raise ValueError(
            "SKILL.md field 'enable-hooks' is not supported; Skills never authorize "
            f"Hook execution: {file_path}"
        )

    metadata = SkillMetadata(
        name=name,
        description=description,
        version=version,
        allowed_tools=allowed_tools,
        argument_hint=_optional_str(data.get("argument-hint")),
        arguments=_parse_string_list(data.get("arguments"), field_name="arguments"),
        when_to_use=_optional_str(data.get("when_to_use")),
        model=_optional_str(data.get("model")),
        context=_parse_context(data.get("context")),
        agent=_optional_str(data.get("agent")),
        effort=_optional_str(data.get("effort")),
    )

    return metadata, markdown_body


def _validate_skill_name(name: str, file_path: str) -> None:
    if len(name) > _MAX_SKILL_NAME_LENGTH:
        raise ValueError(f"Skill name exceeds {_MAX_SKILL_NAME_LENGTH} characters: {file_path}")
    if not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError(
            "Skill name must use lowercase kebab-case with letters, digits, and single hyphens: "
            f"{file_path}"
        )


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_string_list(value: Any, *, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return items or None
    if isinstance(value, str):
        parts = re.split(r"[,|\n]+", value.strip())
        items = [part.strip() for part in parts if part.strip()]
        return items or None
    raise ValueError(f"Skill field '{field_name}' must be a string or list of strings")


def _parse_context(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        parsed = value.strip()
        if parsed not in {"inline", "fork"}:
            raise ValueError("Skill field 'context' must be 'inline' or 'fork'")
        return parsed
    return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_skills_prompt(
    skills: dict[str, Skill],
) -> str:
    """Build the skills catalogue section for the system prompt.

    Only skills configured with ``load_mode == "on-demand"`` are included.
    Eager skills are injected separately with their full instructions.
    """
    if not skills:
        return ""

    on_demand_skills = [
        s for s in sorted(skills.values(), key=lambda s: s.metadata.name)
        if s.metadata.load_mode == "on-demand"
    ]

    if not on_demand_skills:
        return ""

    skills_xml_parts: list[str] = []
    for skill in on_demand_skills:
        skills_xml_parts.append("<skill>")
        skills_xml_parts.append(f"<name>{skill.metadata.name}</name>")
        skills_xml_parts.append(f"<description>{skill.metadata.description}</description>")
        if skill.metadata.argument_hint:
            skills_xml_parts.append(f"<argument_hint>{skill.metadata.argument_hint}</argument_hint>")
        if skill.metadata.when_to_use:
            skills_xml_parts.append(f"<when_to_use>{skill.metadata.when_to_use}</when_to_use>")
        skills_xml_parts.append("</skill>")
    skills_xml = "\n".join(skills_xml_parts)
    return SKILLS_PROMPT.format(skillsXml=skills_xml)
