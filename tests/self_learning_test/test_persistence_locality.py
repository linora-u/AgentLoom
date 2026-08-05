from __future__ import annotations

import ast
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
_SELF_LEARNING_ROOT = _SOURCE_ROOT / "extensions" / "self_learning"
_PERSISTENCE_ROOT = _SELF_LEARNING_ROOT / "persistence"


def _imports_sqlite_driver(module_path: Path) -> bool:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "sqlite3" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            return True
    return False


def _imports_self_learning(module_path: Path) -> bool:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("src.extensions.self_learning")
            for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("src.extensions.self_learning")
        ):
            return True
    return False


def _contains_sqlite_implementation(module_path: Path) -> bool:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "sqlite3" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            return True
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "executescript"}
        ):
            return True
    return False


def test_sqlite_implementation_is_local_to_persistence_package() -> None:
    offenders = [
        path.relative_to(_SELF_LEARNING_ROOT).as_posix()
        for path in _SELF_LEARNING_ROOT.rglob("*.py")
        if not path.is_relative_to(_PERSISTENCE_ROOT)
        and _contains_sqlite_implementation(path)
    ]

    assert offenders == []


def test_self_learning_sqlite_driver_is_not_imported_by_other_source_modules() -> None:
    offenders = [
        path.relative_to(_SOURCE_ROOT).as_posix()
        for path in _SOURCE_ROOT.rglob("*.py")
        if not path.is_relative_to(_PERSISTENCE_ROOT)
        and _imports_sqlite_driver(path)
        and (
            path.is_relative_to(_SELF_LEARNING_ROOT)
            or _imports_self_learning(path)
        )
    ]

    assert offenders == []


def test_database_owner_is_not_imported_by_production_consumers() -> None:
    offenders: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        if path.is_relative_to(_PERSISTENCE_ROOT):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module
            == "src.extensions.self_learning.persistence.database"
            for node in ast.walk(tree)
        ):
            offenders.append(path.relative_to(_SOURCE_ROOT).as_posix())

    assert offenders == []


def test_database_module_is_the_only_connection_owner() -> None:
    offenders: list[str] = []
    for path in _PERSISTENCE_ROOT.rglob("*.py"):
        if path.name == "database.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            owner = node.func.value
            if (
                node.func.attr == "connect"
                and isinstance(owner, ast.Name)
                and owner.id == "sqlite3"
            ):
                offenders.append(path.relative_to(_PERSISTENCE_ROOT).as_posix())
                break

    assert offenders == []


def test_session_index_forwarder_has_been_absorbed() -> None:
    assert not (_SELF_LEARNING_ROOT / "session_index.py").exists()

    from src.extensions.self_learning.persistence.event_importer import (
        SessionEventImporter,
    )
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger

    assert {"index_run", "index_all"} <= set(dir(SessionEventImporter))
    assert not {"index_run", "index_all"} & set(dir(SelfLearningLedger))
    required_ledger_operations = {
        "search_events",
        "scroll_events",
        "root_run_id_for",
    }
    assert required_ledger_operations <= set(dir(SelfLearningLedger))
