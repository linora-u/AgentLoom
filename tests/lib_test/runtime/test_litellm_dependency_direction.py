"""Dependency-direction checks for provider model adapters."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LITELLM_ADAPTER_ROOT = PROJECT_ROOT / "src" / "adapters" / "litellm"
LEGACY_REEXPORTS = (
    PROJECT_ROOT
    / "src"
    / "adapters"
    / "smolagents"
    / "models"
    / "litellm_retry.py",
    PROJECT_ROOT
    / "src"
    / "adapters"
    / "smolagents"
    / "models"
    / "request_headers.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    return imports


def test_litellm_adapter_does_not_depend_on_smolagents() -> None:
    violations: list[str] = []
    for path in sorted(LITELLM_ADAPTER_ROOT.rglob("*.py")):
        for imported in sorted(_imports(path)):
            if imported == "smolagents" or imported.startswith(
                "agentloom.adapters.smolagents"
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)} imports {imported}"
                )

    assert violations == []


def test_legacy_governance_modules_are_declarative_reexports_only() -> None:
    violations: list[str] = []
    forbidden_nodes = (
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.FunctionDef,
        ast.If,
        ast.Try,
        ast.While,
    )
    for path in LEGACY_REEXPORTS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        leaked = [
            type(node).__name__
            for node in ast.walk(tree)
            if isinstance(node, forbidden_nodes)
        ]
        if leaked:
            violations.append(
                f"{path.relative_to(PROJECT_ROOT)} contains {sorted(set(leaked))}"
            )

    assert violations == []
