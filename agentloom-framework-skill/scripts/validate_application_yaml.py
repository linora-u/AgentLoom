#!/usr/bin/env python3
"""Validate Application authoring structure through the shared definition preflight.

Public CLI:
    .venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
      --app-root applications/<app_name>

This adapter owns directory discovery and the JSON envelope only. Definition,
Worker, model, configuration, and reference rules belong to agentloom.application.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from agentloom.application.definition import (
    definition_error,
    inspect_application_definition,
    read_agent_definition,
)
from agentloom.application.readiness import validate_runtime_worker_config
from agentloom.configuration.config import load_project_config

_AGENT_EXTENSIONS = {".yaml", ".yml", ".md"}


def _discover_project_root(start: Path) -> Path | None:
    candidates = [start.resolve(), *start.resolve().parents]
    # Application overlays can also have system.yaml; prefer the model catalog
    # at the project root. The fallback lets canonical preflight report its absence.
    for filename in ("llm.yaml", "system.yaml"):
        for candidate in candidates:
            if (candidate / "config" / filename).is_file():
                return candidate
    return None


def _collect_agent_files(workflows_dir: Path) -> list[Path]:
    supervisors = sorted(
        path.resolve()
        for path in workflows_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _AGENT_EXTENSIONS
    )
    worker_dir = workflows_dir / "worker_agents"
    workers = sorted(
        path.resolve()
        for path in worker_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in _AGENT_EXTENSIONS
    )
    return list(dict.fromkeys([*supervisors, *workers]))


def _error(path: Path, message: str, *, root: Path, field: str = "definition", rule: str = "shared_definition") -> dict[str, str]:
    try:
        display_path = str(path.resolve().relative_to(root))
    except ValueError:
        display_path = str(path.resolve())
    return {
        "file": display_path,
        "field": field,
        "rule": rule,
        "message": message,
        "suggestion": "按共享定义校验给出的原因修正配置后重试",
    }


def _emit(app_root: Path | str, errors: list[dict[str, str]], *, files_checked: int = 0, config_files_checked: int = 0) -> None:
    print(json.dumps({
        "summary": {
            "valid": not errors,
            "error_count": len(errors),
            "files_checked": files_checked,
            "config_files_checked": config_files_checked,
            "app_root": str(app_root),
        },
        "errors": errors,
    }, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate AgentLoom Application definitions with shared preflight.")
    parser.add_argument("--app-root", required=True, help="Application root path, e.g. applications/code_review")
    args = parser.parse_args()
    project_root = _discover_project_root(Path.cwd())
    if project_root is None:
        _emit(args.app_root, [_error(
            Path.cwd(), "未找到 config/llm.yaml 或 config/system.yaml，无法定位项目根目录",
            root=Path.cwd(), field="project_root", rule="project_root_discovery",
        )])
        return 2

    app_root = Path(args.app_root).expanduser()
    app_root = (project_root / app_root).resolve()
    workflows_dir = app_root / "workflows"
    for path, field in ((app_root, "app_root"), (workflows_dir, "workflows")):
        if not path.is_dir():
            _emit(app_root, [_error(
                path, f"缺少目录: {path}", root=project_root, field=field, rule="path_exists",
            )])
            return 1

    files = _collect_agent_files(workflows_dir)
    config_files_checked = int((app_root / "config/system.yaml").is_file())
    if not files:
        _emit(app_root, [_error(
            workflows_dir, "workflows 中没有 Agent YAML/Markdown 定义", root=project_root,
            field="workflows", rule="definition_required",
        )], config_files_checked=config_files_checked)
        return 1

    cache = {}
    errors: list[dict[str, str]] = []
    seen_messages: set[str] = set()

    def add(path: Path, message: str) -> None:
        if message not in seen_messages:
            seen_messages.add(message)
            errors.append(_error(path, message, root=project_root))

    parsed = {}
    for path in files:
        read = read_agent_definition(path, cache=cache)
        if read.error is not None:
            add(path, read.error)
        elif read.definition is not None:
            parsed[path] = read.definition

    try:
        base = load_project_config(project_root)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
        add(project_root / "config", f"{project_root}/config: {definition_error(exc)}")
        base = None
    visited: set[Path] = set()
    worker_dir = (workflows_dir / "worker_agents").resolve()
    for path, definition in parsed.items():
        if path in visited:
            continue
        # Referenced Workers are validated as Workers by the shared walk.
        # Authoring also checks unreferenced definitions in worker_agents/.
        if path.is_relative_to(worker_dir):
            try:
                validate_runtime_worker_config(definition, path, agent_root=project_root)
            except (TypeError, ValueError) as exc:
                add(path, f"{path}: {definition_error(exc)}")
        inspection = inspect_application_definition(
            project_root, str(path), definition, base_config=base, definition_cache=cache,
        )
        visited.update(inspection.definitions)
        for message in inspection.errors:
            add(path, message)

    _emit(
        app_root, errors,
        files_checked=sum(read.definition is not None for read in cache.values()),
        config_files_checked=config_files_checked,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
        _emit("", [_error(
            Path.cwd(), definition_error(exc), root=Path.cwd(), field="runtime", rule="unexpected_exception",
        )])
        sys.exit(2)
