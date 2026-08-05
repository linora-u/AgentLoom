"""Lightweight Agent definition loading and catalog validation.

The TUI index needs the same configuration truth as the runtime, but must not
import Agent classes, model providers, or concrete tool implementations. This module is
the shared observation boundary for YAML/Markdown definitions.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from src.lib.smolagents.agent.agent_validation import AgentConfigNormalizer
from src.lib.smolagents.agent.runtime_validation import (
    validate_runtime_agent_config,
    validate_runtime_worker_config,
)

_MARKDOWN_YAML = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


@dataclass(frozen=True)
class AgentDefinitionRead:
    """One reusable definition read, including a stable parse failure."""

    definition: dict[str, object] | None
    error: str | None


type AgentDefinitionCache = MutableMapping[Path, AgentDefinitionRead]


def load_agent_definition(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".md":
        match = _MARKDOWN_YAML.search(content)
        if match is None:
            raise ValueError("No YAML code block found in markdown file")
        raw = yaml.safe_load(match.group(1))
        if not isinstance(raw, dict):
            raise ValueError("Agent configuration must be a mapping")
        workflow = _MARKDOWN_YAML.sub("", content).strip()
        if workflow:
            raw["workflow"] = workflow
    elif path.suffix.lower() in {".yaml", ".yml"}:
        raw = yaml.safe_load(content)
    else:
        raise ValueError(f"Unsupported file format: {path.suffix}")
    if not isinstance(raw, dict):
        raise ValueError("Agent configuration must be a mapping")
    prepared = copy.deepcopy(raw)
    prepared["_yaml_file_path"] = str(path.resolve())
    return prepared


def read_agent_definition(
    path: Path,
    *,
    cache: AgentDefinitionCache | None = None,
) -> AgentDefinitionRead:
    """Read a definition once per cache, preserving valid and invalid results."""

    try:
        cache_key = path.resolve()
    except OSError:
        cache_key = path.absolute()
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    try:
        definition = load_agent_definition(path)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
        result = AgentDefinitionRead(definition=None, error=str(error))
    else:
        result = AgentDefinitionRead(definition=definition, error=None)
    if cache is not None:
        cache[cache_key] = result
    return result


def model_types(project_root: Path) -> tuple[str, dict[str, object]]:
    path = project_root / "config" / "llm.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return "", {}
    model = raw.get("model", {}) if isinstance(raw, dict) else {}
    if not isinstance(model, dict):
        return "", {}
    default = model.get("default_model_type")
    return (default.strip() if isinstance(default, str) else ""), model


def _validate_model_reference(
    source_path: Path | str,
    parsed: dict[str, object],
    catalog: tuple[str, dict[str, object]],
) -> list[str]:
    default_model, models = catalog
    selected_model = parsed.get("model_type", default_model)
    if not isinstance(selected_model, str) or not selected_model.strip():
        return [f"{source_path}: no model_type and no configured default model"]
    selected_model = selected_model.strip()
    model_settings = models.get(selected_model)
    if not isinstance(model_settings, dict) or not model_settings.get("model"):
        return [f"{source_path}: model_type '{selected_model}' is not configured"]
    return []


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Agent path escapes the project root") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Agent path contains a symlink: {current}")


def _validate_worker_references(
    project_root: Path,
    relative_path: str,
    parsed: dict[str, object],
    draft_paths: set[str],
    draft_configs: Mapping[str, dict[str, object]],
    catalog: tuple[str, dict[str, object]],
    definition_cache: AgentDefinitionCache | None,
) -> list[str]:
    raw_workers = parsed.get("worker_agents", [])
    try:
        AgentConfigNormalizer.validate_worker_agents_config(raw_workers)
    except ValueError as exc:
        return [str(exc)]
    if not raw_workers:
        return []

    supervisor = project_root.joinpath(*PurePosixPath(relative_path).parts)
    worker_folder = supervisor.parent / "worker_agents"
    errors: list[str] = []
    folder_is_staged = any(
        project_root.joinpath(*PurePosixPath(path).parts).parent == worker_folder for path in draft_paths
    )
    if not worker_folder.is_dir() and not folder_is_staged:
        errors.append(f"worker_agents folder not found: {worker_folder}")
    for item in raw_workers:
        configured_path = str(item["path"]).strip()
        try:
            candidate = AgentConfigNormalizer.resolve_worker_agent_config_path(
                configured_path,
                worker_folder,
                agent_root=project_root,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if candidate.suffix.lower() not in {".yaml", ".yml", ".md"}:
            errors.append(f"worker_agents path '{configured_path}' resolved to '{candidate}' has unsupported extension")
            continue
        try:
            candidate_relative = candidate.relative_to(project_root).as_posix()
        except ValueError:
            candidate_relative = None
        if candidate_relative in draft_paths:
            worker_config = draft_configs.get(candidate_relative)
            if worker_config is None:
                continue
            try:
                validate_runtime_worker_config(
                    worker_config,
                    candidate_relative,
                    agent_root=project_root,
                )
                errors.extend(_validate_model_reference(candidate_relative, worker_config, catalog))
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"worker_agents path '{configured_path}' resolved to '{candidate_relative}' is invalid: {exc}"
                )
            continue
        try:
            if candidate_relative is not None:
                _reject_symlink_components(project_root, project_root / candidate_relative)
            if not candidate.is_file():
                errors.append(
                    f"worker_agents path '{configured_path}' resolved to '{candidate}' does not exist or is not a file"
                )
                continue
            worker_read = read_agent_definition(candidate, cache=definition_cache)
            if worker_read.error is not None or worker_read.definition is None:
                raise ValueError(worker_read.error or "agent definition must be a YAML object")
            worker_config = worker_read.definition
            validate_runtime_worker_config(
                worker_config,
                candidate,
                agent_root=project_root,
            )
            errors.extend(_validate_model_reference(candidate, worker_config, catalog))
        except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
            errors.append(f"worker_agents path '{configured_path}' resolved to '{candidate}' is invalid: {exc}")
    return errors


def validate_agent_definition(
    project_root: Path,
    relative_path: str,
    parsed: dict[str, object],
    *,
    draft_paths: set[str] | None = None,
    draft_configs: Mapping[str, dict[str, object]] | None = None,
    catalog: tuple[str, dict[str, object]] | None = None,
    definition_cache: AgentDefinitionCache | None = None,
) -> list[str]:
    """Validate one parsed definition without constructing an Agent runtime."""

    errors: list[str] = []
    try:
        validate_runtime_agent_config(parsed, relative_path, agent_root=project_root)
    except ValueError as exc:
        errors.append(str(exc))
    for key in ("runtime", "logging"):
        if key in parsed:
            errors.append(f"{relative_path}: '{key}' is global-only and is not allowed in Agent YAML")

    resolved_catalog = catalog or model_types(project_root)
    errors.extend(_validate_model_reference(relative_path, parsed, resolved_catalog))
    errors.extend(
        f"{relative_path}: {error}"
        for error in _validate_worker_references(
            project_root,
            relative_path,
            parsed,
            draft_paths or set(),
            draft_configs or {},
            resolved_catalog,
            definition_cache,
        )
    )
    return errors
