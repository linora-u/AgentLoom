"""Model-facing Skill activation tool."""

from __future__ import annotations

import html

from src.lib.smolagents.skills.catalog import SkillCatalog
from src.trace.task_context import get_current_skill_catalog


def _resolve_catalog() -> SkillCatalog:
    catalog = get_current_skill_catalog()
    if not isinstance(catalog, SkillCatalog):
        raise RuntimeError("skill tool requires an explicitly bound SkillCatalog")
    return catalog


def skill(name: str) -> str:
    """Load one available Skill into the current conversation.

    Args:
        name: Exact name from the available skills catalogue.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required and must be a non-empty string")

    activation = _resolve_catalog().activate(name.strip())
    escaped_name = html.escape(activation.name, quote=True)
    parts = [
        f'<skill_content name="{escaped_name}">',
        f"# Skill: {activation.name}",
        "",
        activation.instructions.rstrip(),
        "",
        f"Base directory for this skill: {activation.directory}",
        "Relative paths in this skill are relative to this base directory.",
        "The file list is sampled; use the normal file and shell tools under the Agent's existing permissions.",
        "",
        "<skill_files>",
        *(f"<file>{html.escape(str(path))}</file>" for path in activation.files),
        "</skill_files>",
        "</skill_content>",
    ]
    return "\n".join(parts)
