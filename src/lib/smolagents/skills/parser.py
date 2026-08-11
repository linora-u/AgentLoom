"""Skill file parsing and prompt generation.

AgentLoom skills use OpenCode-compatible ``SKILL.md`` packages.  The parser is
strict about the required contract (valid YAML frontmatter with ``name`` and
``description``) and intentionally ignores unknown frontmatter fields instead
of mapping legacy aliases.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import frontmatter

from src.lib.logging import get_logger

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_NAME_LENGTH = 64
_MAX_SKILL_DESCRIPTION_LENGTH = 1024


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

SKILLS_PROMPT = """
====

Skills provide specialized instructions and workflows for specific tasks.
Use the skill tool to load a skill when the current task matches its description.

<available_skills>
{skillsXml}
</available_skills>
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
        raise ValueError(f"Skill description exceeds {_MAX_SKILL_DESCRIPTION_LENGTH} characters: {file_path}")

    if "hooks" in data:
        raise ValueError(
            "SKILL.md field 'hooks' is not supported; configure a direct Hook "
            f"or standalone Hook Bundle instead: {file_path}"
        )
    if "enable-hooks" in data:
        raise ValueError(
            f"SKILL.md field 'enable-hooks' is not supported; Skills never authorize Hook execution: {file_path}"
        )

    custom_metadata = data.get("metadata")
    if custom_metadata is not None and not isinstance(custom_metadata, dict):
        raise ValueError(f"Skill field 'metadata' must be a YAML mapping: {file_path}")

    metadata = SkillMetadata(
        name=name,
        description=description,
        license=_optional_str(data.get("license")),
        compatibility=_optional_str(data.get("compatibility")),
        metadata=dict(custom_metadata) if custom_metadata is not None else None,
    )

    return metadata, markdown_body


def _validate_skill_name(name: str, file_path: str) -> None:
    if len(name) > _MAX_SKILL_NAME_LENGTH:
        raise ValueError(f"Skill name exceeds {_MAX_SKILL_NAME_LENGTH} characters: {file_path}")
    if not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Skill name must use lowercase kebab-case with letters, digits, and single hyphens: {file_path}"
        )


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_skills_prompt(summaries) -> str:
    """Build the model-visible catalogue from resolved Skill summaries."""
    summaries = tuple(summaries)
    if not summaries:
        return ""

    skills_xml_parts: list[str] = []
    for skill in sorted(summaries, key=lambda item: item.name):
        skills_xml_parts.append("<skill>")
        skills_xml_parts.append(f"<name>{html.escape(skill.name)}</name>")
        skills_xml_parts.append(f"<description>{html.escape(skill.description)}</description>")
        skills_xml_parts.append("</skill>")
    skills_xml = "\n".join(skills_xml_parts)
    return SKILLS_PROMPT.format(skillsXml=skills_xml)
