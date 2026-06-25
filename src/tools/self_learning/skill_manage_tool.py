"""Proposal-only skill management tool."""

from __future__ import annotations

import json

from src.extensions.self_learning.proposal_writer import ProposalWriter


def skill_manage(
    action: str,
    name: str,
    content: str = "",
    old_string: str = "",
    new_string: str = "",
    path: str = "",
    target: str = "",
) -> str:
    """Create proposal-only skill changes under skills/generated/proposals.

    This tool never edits active skills. Promote proposals explicitly with
    ``loom skills proposals promote`` after review.

    Args:
        action: create, patch, edit, archive, write_file, or remove_file.
        name: Proposed skill name.
        content: New skill or file content.
        old_string: Existing text for patch/edit proposals.
        new_string: Replacement text for patch/edit proposals.
        path: Relative file path for write_file/remove_file proposals.
        target: Optional target active skill or file description.
    """
    result = ProposalWriter().handle_tool_action(
        action,
        name=name,
        content=content,
        old_string=old_string,
        new_string=new_string,
        path=path,
        target=target,
    )
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)

