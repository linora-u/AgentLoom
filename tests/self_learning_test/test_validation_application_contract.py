from __future__ import annotations

from pathlib import Path

import yaml

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


def test_self_learning_smoke_declares_a_bounded_tool_sequence() -> None:
    workflow_path = (
        REPOSITORY_ROOT
        / "applications/self_learning_smoke/workflows/self_learning_smoke_agent.yaml"
    )
    config = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    workflow = config["workflow"]

    assert config["max_steps"] == 16
    assert config["todo"] == {"mode": "off"}
    assert [tool["name"] for tool in config["tools"]] == [
        "session_search",
        "memory",
        "skill_manage",
        "shell_tool",
        "read_file",
    ]
    assert "successful search." in workflow
    assert "do not repeat the call after `ok=true`" in workflow
    assert "non-empty `content` argument" in workflow
    assert '`path="references/run.json"`' in workflow
    assert "do not inspect the proposal" in workflow
    assert "Do not use shell_tool or read_file to inspect the proposal." in workflow
