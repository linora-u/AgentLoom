from __future__ import annotations

from pathlib import Path

import pytest
from agentloom.self_learning.proposal_writer import ProposalWriter


def test_create_requires_complete_skill_content_before_writing_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    proposals = tmp_path / "proposals"
    writer = ProposalWriter(
        proposals_dir=proposals,
        skills_dir=tmp_path / "skills",
    )

    with pytest.raises(
        ValueError,
        match="create requires non-empty SKILL.md content",
    ):
        writer.create(
            action="create",
            name="self-learning-smoke",
            content="",
        )

    assert list(proposals.iterdir()) == []
