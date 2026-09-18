from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_multi_agent_memory_worker_uses_the_public_proposal_contract() -> None:
    workflow = (
        REPOSITORY_ROOT
        / "applications/self_learning_multi_agent/workflows/worker_agents/memory_reviewer.yaml"
    ).read_text(encoding="utf-8")

    assert "`action=propose`" in workflow
    assert "`scope=app`" in workflow
    assert "candidate id" in workflow
    assert "`pending=true`" in workflow
    assert "`state=pending_pre_review`" in workflow
    assert "action=add" not in workflow
    assert "scope=project" not in workflow
