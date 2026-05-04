"""Skill file parsing and prompt generation.

Extracts metadata and markdown body from SKILL.md / skill.md files
with YAML frontmatter, and builds the skills catalogue prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import frontmatter

from src.lib.config.config_validation import BoolParser
from src.lib.logging import get_logger

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SkillMetadata:
    name: str
    description: str
    version: Optional[str] = None
    invocation_control: Optional[Dict[str, Any]] = None
    allowed_tools: Optional[List[str]] = None
    hooks: Optional[Dict[str, Any]] = None
    platform: Optional[str] = None

    def __post_init__(self):
        if self.invocation_control is None:
            self.invocation_control = {"allow-model": True, "allow-hook": True}


@dataclass
class Skill:
    metadata: SkillMetadata
    content: Optional[str]
    file_path: str
    hooks_registered: bool = False


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

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        post = frontmatter.loads(content)
        data = post.metadata or {}
        markdown_body = post.content
        if content.endswith(("\n", "\r\n")) and not markdown_body.endswith(("\n", "\r\n")):
            markdown_body += "\n"
    except Exception as e:
        logger.warning(
            "Failed to parse YAML in %s; treating as markdown only: %s",
            file_path,
            e,
        )
        data = {}
        markdown_body = content

    if data is None:
        data = {}

    if not isinstance(data, dict):
        logger.warning(
            "Skill in %s has invalid frontmatter (expected mapping); treating as markdown only",
            file_path,
        )
        data = {}
        markdown_body = content

    name = data.get("name")
    if not isinstance(name, str) or not name:
        name = Path(file_path).parent.name

    description = data.get("description", "")
    if not isinstance(description, str):
        description = ""

    version = data.get("version")
    if not isinstance(version, str):
        version = None

    allowed_tools_raw = data.get("allowed-tools")
    allowed_tools: Optional[List[str]] = None
    if isinstance(allowed_tools_raw, list):
        if all(isinstance(tool, str) for tool in allowed_tools_raw):
            allowed_tools = allowed_tools_raw
    elif isinstance(allowed_tools_raw, str):
        parts = re.split(r"[,|\s]+", allowed_tools_raw.strip())
        allowed_tools = [t for t in parts if t] or None

    hooks_raw = data.get("hooks")
    hooks = hooks_raw if isinstance(hooks_raw, dict) else None

    metadata = SkillMetadata(
        name=name,
        description=description,
        version=version,
        allowed_tools=allowed_tools,
        hooks=hooks,
    )

    return metadata, markdown_body


# ---------------------------------------------------------------------------
# Invocation-control parsing (reference-site config)
# ---------------------------------------------------------------------------

def parse_invocation_control(
    raw: Any,
    *,
    logger: Any = None,
) -> Dict[str, Any]:
    """Parse an ``invocation-control`` dict from Agent YAML / system.yaml.

    Returns a normalised dict with keys ``"allow-model"`` (tri-state:
    ``True`` | ``False`` | ``"force-inject"``) and ``"allow-hook"`` (bool).

    When *raw* is not a dict, returns the default
    ``{"allow-model": True, "allow-hook": True}``.
    """
    logger = get_logger(logger, __name__)

    if not isinstance(raw, dict):
        logger.warning(
            "invocation-control value is not a dict (%r); using defaults",
            raw,
        )
        return {"allow-model": True, "allow-hook": True}

    result: Dict[str, Any] = {}

    # allow-model: tri-state — True | False | "force-inject"
    _raw_allow_model = raw.get("allow-model", True)
    if isinstance(_raw_allow_model, str) and _raw_allow_model.strip().lower() in (
        "force-inject", "force_inject", "inject",
    ):
        result["allow-model"] = "force-inject"
    else:
        result["allow-model"] = BoolParser.parse(
            _raw_allow_model,
            default=True,
            field_name="invocation-control.allow-model",
            logger=logger,
        )

    # allow-hook: boolean
    result["allow-hook"] = BoolParser.parse(
        raw.get("allow-hook", True),
        default=True,
        field_name="invocation-control.allow-hook",
        logger=logger,
    )

    return result


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_skills_prompt(
    skills: Dict[str, Skill],
) -> str:
    """Build the skills catalogue section for the system prompt.

    Only skills with ``allow-model: true`` (on-demand) are included.
    Skills with ``allow-model: false`` (hidden) or
    ``allow-model: "force-inject"`` are excluded.
    """
    if not skills:
        return ""

    # Only on-demand skills (allow-model is exactly True) go into the catalogue
    on_demand_skills = [
        s for s in sorted(skills.values(), key=lambda s: s.metadata.name)
        if s.metadata.invocation_control.get("allow-model", True) is True
    ]

    # If every skill is force-injected, no catalogue or mandatory check needed
    if not on_demand_skills:
        return ""

    skills_xml_parts: list[str] = []
    for skill in on_demand_skills:
        skills_xml_parts.append("<skill>")
        skills_xml_parts.append(f"<name>{skill.metadata.name}</name>")
        skills_xml_parts.append(f"<description>{skill.metadata.description}</description>")
        skills_xml_parts.append("</skill>")
    skills_xml = "\n".join(skills_xml_parts)
    return SKILLS_PROMPT.format(skillsXml=skills_xml)
