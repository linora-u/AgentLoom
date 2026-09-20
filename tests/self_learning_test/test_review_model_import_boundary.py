"""Review model orchestration must stay on the canonical model-turn seam."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVIEW_ORCHESTRATION = (
    PROJECT_ROOT / "src" / "self_learning" / "review_orchestration.py"
)


def test_review_orchestration_has_no_smolagents_or_legacy_model_imports() -> None:
    source = REVIEW_ORCHESTRATION.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(REVIEW_ORCHESTRATION))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")

    forbidden = sorted(
        imported
        for imported in imports
        if imported == "smolagents"
        or imported.startswith("agentloom.adapters.smolagents")
    )
    assert forbidden == []
    assert "model_manager" not in source
    assert "ChatMessage" not in source
    assert "MessageRole" not in source


def test_review_orchestration_uses_canonical_binding_and_items() -> None:
    source = REVIEW_ORCHESTRATION.read_text(encoding="utf-8")

    assert "ModelTurnBinding" in source
    assert 'MessageItem(role="system"' in source
    assert 'MessageItem(role="user"' in source
    assert "model.turn(" in source
    assert "limit_provider_calls(1)" in source
