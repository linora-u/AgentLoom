#!/usr/bin/env python3
"""Validate Application authoring structure through the shared definition preflight.

Public CLI:
    .venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
      --app-root applications/<app_name>

Applications that load a local model catalog at runtime can pass
``--model-config config/model.yaml`` (relative to ``--app-root``) so preflight
checks the same model references.

This adapter owns directory discovery, optional runtime model-catalog overlay,
and the JSON envelope. Definition, Worker, model, configuration, and reference
rules belong to agentloom.app.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from agentloom.app.definition import (
    definition_error,
    discover_application_definition_files,
    inspect_application_definition,
    read_agent_definition,
)
from agentloom.config.config import load_project_config
from agentloom.config.llm_config import LLMConfig
from agentloom.config.yaml_loader import load_unique_yaml


def _discover_project_root(start: Path) -> Path | None:
    candidates = [start.resolve(), *start.resolve().parents]
    # The nearest AgentLoom config directory owns validation. If llm.yaml is
    # missing there, shared preflight should report that absence instead of
    # accidentally borrowing a model catalog from a parent temp/workspace tree.
    for candidate in candidates:
        config_dir = candidate / "config"
        if (config_dir / "llm.yaml").is_file() or (config_dir / "system.yaml").is_file():
            return candidate
    return None


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
    parser.add_argument("--model-config", help="Optional model catalog path relative to --app-root")
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

    discovered = discover_application_definition_files(workflows_dir)
    files = [item.path for item in discovered]
    roles = {item.path: item.role for item in discovered}
    config_files_checked = int((app_root / "config/system.yaml").is_file())
    if not files:
        _emit(app_root, [_error(
            workflows_dir, "workflows 中没有 Agent YAML 定义", root=project_root,
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
    if base is not None and args.model_config:
        model_path = app_root / args.model_config
        try:
            if not model_path.resolve().is_relative_to(app_root):
                raise ValueError("--model-config must be inside --app-root")
            local_raw = load_unique_yaml(model_path.read_text(encoding="utf-8")) or {}
            local_models = local_raw.get("model") if isinstance(local_raw, dict) else None
            if not isinstance(local_models, dict) or not local_models:
                raise ValueError("local model catalog must define at least one model")
            merged_raw = load_unique_yaml((project_root / "config/llm.yaml").read_text(encoding="utf-8")) or {}
            if not isinstance(merged_raw, dict) or not isinstance(merged_raw.get("model"), dict):
                raise ValueError("project model catalog must contain a model mapping")
            merged_raw["model"].update(local_models)
            merged = LLMConfig.from_dict(merged_raw)
            for name in local_models:
                if name not in merged.models:
                    raise ValueError(f"local model catalog entry {name!r} is not a model profile")
                base.llm.models[name] = merged.models[name]
            config_files_checked += 1
        except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
            add(model_path, f"{model_path}: {definition_error(exc)}")
    visited: set[Path] = set()
    for path, definition in parsed.items():
        if path in visited:
            continue
        inspection = inspect_application_definition(
            project_root,
            str(path),
            definition,
            root_is_worker=roles[path] == "worker",
            base_config=base,
            definition_cache=cache,
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
