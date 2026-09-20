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


@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        ("", '{"marker":"one"}', "write_file requires a relative path"),
        ("references/run.json", "", "write_file requires non-empty content"),
    ],
)
def test_write_file_rejects_incomplete_input_without_mutating_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    content: str,
    message: str,
) -> None:
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    proposals = tmp_path / "proposals"
    writer = ProposalWriter(
        proposals_dir=proposals,
        skills_dir=tmp_path / "skills",
    )
    created = writer.create(
        action="create",
        name="self-learning-smoke",
        content="# Smoke\n",
    )
    proposal_path = Path(created["proposal_path"])
    before = {
        item.relative_to(proposal_path): item.read_bytes()
        for item in proposal_path.rglob("*")
        if item.is_file()
    }

    with pytest.raises(ValueError, match=message):
        writer.create(
            action="write_file",
            name="self-learning-smoke",
            path=path,
            content=content,
        )

    after = {
        item.relative_to(proposal_path): item.read_bytes()
        for item in proposal_path.rglob("*")
        if item.is_file()
    }
    assert after == before
