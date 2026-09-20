"""Shared, side-effect-free Application definition and topology semantics.

Only data is read here. Model construction, tool imports, MCP connections, and
Hook execution belong to the execution adapter after preflight succeeds.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from agentloom.application.readiness import (
    validate_runtime_agent_config,
    validate_runtime_worker_config,
)
from agentloom.application.validation import AgentConfigNormalizer
from agentloom.configuration.config import (
    EffectiveAgentConfigSnapshot,
    UnifiedConfig,
    build_effective_agent_config_snapshot,
    load_project_config,
)
from agentloom.configuration.llm_config import LLMConfig
from agentloom.configuration.yaml_loader import load_unique_yaml
from pydantic import ValidationError

if TYPE_CHECKING:
    from agentloom.runtime.skills.catalog import SkillCatalog

_MARKDOWN_YAML = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)
_AGENT_DEFINITION_EXTENSIONS = frozenset({".yaml", ".yml", ".md"})


@dataclass(frozen=True)
class AgentDefinitionRead:
    definition: dict[str, object] | None
    error: str | None


type AgentDefinitionCache = MutableMapping[Path, AgentDefinitionRead]


@dataclass(frozen=True)
class ApplicationDefinitionInspection:
    """One static read of a topology, including partially invalid definitions."""

    definitions: dict[Path, dict[str, object]]
    snapshots: dict[Path, EffectiveAgentConfigSnapshot]
    errors: tuple[str, ...]
    errors_by_path: dict[Path, tuple[str, ...]]


@dataclass(frozen=True)
class DiscoveredAgentDefinition:
    """One Application definition found below workflows/, with its file role."""

    path: Path
    role: Literal["supervisor", "worker"]


def discover_application_definition_files(
    workflows_dir: Path | str,
) -> tuple[DiscoveredAgentDefinition, ...]:
    """Recursively find YAML/Markdown definitions without following symlinks.

    A definition below any ``worker_agents`` directory is a Worker. Every other
    definition below ``workflows`` is a Supervisor, including definitions in
    nested workflow groups.
    """

    configured_root = Path(workflows_dir).expanduser()
    if configured_root.is_symlink():
        return ()
    root = configured_root.resolve()
    if not root.is_dir():
        return ()

    definitions: list[DiscoveredAgentDefinition] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current_path / name
            if (
                path.suffix.lower() not in _AGENT_DEFINITION_EXTENSIONS
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            relative = path.relative_to(root)
            role: Literal["supervisor", "worker"] = (
                "worker" if "worker_agents" in relative.parts[:-1] else "supervisor"
            )
            definitions.append(DiscoveredAgentDefinition(path.resolve(), role))
    return tuple(definitions)


def extract_markdown_definition(content: str) -> tuple[dict[str, Any], str]:
    match = _MARKDOWN_YAML.search(content)
    if match is None:
        raise ValueError("No YAML code block found in markdown file")
    raw = load_unique_yaml(match.group(1))
    if not isinstance(raw, dict):
        raise ValueError("Agent configuration must be a mapping")
    workflow = _MARKDOWN_YAML.sub("", content).strip()
    if workflow:
        raw["workflow"] = workflow
    return raw, workflow


def load_agent_definition(path: Path | str) -> dict[str, object]:
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".md":
        raw, _ = extract_markdown_definition(content)
    elif path.suffix.lower() in {".yaml", ".yml"}:
        raw = load_unique_yaml(content)
    else:
        raise ValueError(f"Unsupported file format: {path.suffix}")
    if not isinstance(raw, dict):
        raise ValueError("Agent configuration must be a mapping")
    prepared = copy.deepcopy(raw)
    prepared["_yaml_file_path"] = str(path.resolve())
    return prepared


def definition_error(error: BaseException) -> str:
    """Preserve location and reason without echoing credential-bearing input."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
    if isinstance(error, yaml.MarkedYAMLError):
        mark = error.problem_mark
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"{error.problem or 'Invalid YAML'}{location}"
    return str(error)


def read_agent_definition(path: Path, *, cache: AgentDefinitionCache | None = None) -> AgentDefinitionRead:
    """Read once within one inspection/Run; callers create a cache per request."""
    key = path.resolve()
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    try:
        result = AgentDefinitionRead(load_agent_definition(path), None)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
        result = AgentDefinitionRead(None, f"{path}: {definition_error(error)}")
    if cache is not None:
        cache[key] = copy.deepcopy(result)
    return result


def model_types(project_root: Path) -> tuple[str, dict[str, object]]:
    """Return the catalog used by model selection, without constructing a model."""
    try:
        raw = load_unique_yaml((project_root / "config/llm.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError):
        return "", {}
    model = raw.get("model", {}) if isinstance(raw, dict) else {}
    if not isinstance(model, dict):
        return "", {}
    default = model.get("default_model_type")
    return (default.strip() if isinstance(default, str) else ""), model


def model_catalog(base: UnifiedConfig) -> tuple[str, dict[str, object]]:
    return base.llm.default_model_type, {name: settings.model_dump() for name, settings in base.llm.models.items()}


def selected_model_type(parsed: Mapping[str, Any], catalog: tuple[str, dict[str, object]]) -> str:
    default, models = catalog
    requested = parsed.get("model_type")
    if requested is not None and not isinstance(requested, str):
        raise ValueError("model_type must be a string when provided")
    # for_type owns case normalization, empty selection fallback, and diagnostics.
    config = LLMConfig.model_construct(
        default_model_type=default,
        models={
            key: value
            for key, value in models.items()
            if key not in {"default_model_type", "common"} and isinstance(value, dict)
        },
    )
    selected = config.for_type(requested)
    desired = (requested or "").strip().lower() or default.strip().lower()
    if not selected.get("model"):
        raise ValueError(f"model_type '{desired}' is not configured with a model")
    return desired


def _validate_model_reference(source_path, parsed, catalog) -> list[str]:
    try:
        selected_model_type(parsed, catalog)
    except (TypeError, ValueError) as exc:
        # Preserve the previous concise catalog diagnostic as well as the shared
        # selector's actionable message for existing bridge consumers.
        selected = parsed.get("model_type")
        prefix = f"model_type '{selected}' is not configured: " if selected else ""
        return [f"{source_path}: {prefix}{definition_error(exc)}"]
    return []


def resolve_worker_path(project_root: Path, source_path: Path, configured_path: str) -> Path:
    return AgentConfigNormalizer.resolve_worker_agent_config_path(
        configured_path,
        source_path.parent / "worker_agents",
        agent_root=project_root,
    )


def _walk_definitions(
    project_root: Path,
    source_path: Path,
    parsed: dict[str, object],
    *,
    root_is_worker: bool,
    catalog: tuple[str, dict[str, object]],
    base: UnifiedConfig | None,
    draft_paths: set[str],
    draft_configs: Mapping[str, dict[str, object]],
    cache: AgentDefinitionCache,
) -> ApplicationDefinitionInspection:
    nodes: dict[Path, dict[str, object]] = {}
    snapshots: dict[Path, EffectiveAgentConfigSnapshot] = {}
    errors: list[str] = []
    errors_by_path: dict[Path, tuple[str, ...]] = {}
    active: list[Path] = []

    def visit(path: Path, config: dict[str, object], *, worker: bool) -> None:
        path = path.resolve()
        if path in active:
            errors.append("Worker reference cycle: " + " -> ".join(str(p) for p in [*active, path]))
            return
        if path in nodes:
            return
        config = copy.deepcopy(config)
        config["_yaml_file_path"] = str(path)
        nodes[path] = config
        active.append(path)
        error_start = len(errors)
        try:
            validator = validate_runtime_worker_config if worker else validate_runtime_agent_config
            try:
                validator(config, path, agent_root=project_root)
            except (TypeError, ValueError) as exc:
                errors.append(f"{path}: {definition_error(exc)}")
            errors.extend(_validate_model_reference(path, config, catalog))
            if base is not None:
                try:
                    snapshots[path] = build_effective_agent_config_snapshot(
                        config,
                        source_name=str(path),
                        base_config=base,
                    )
                    AgentConfigNormalizer.validate_agent_runtime_config(
                        config,
                        effective_config=snapshots[path].values,
                    )
                    from agentloom.application.runtime_options import normalize_runtime_options

                    normalize_runtime_options(config, snapshot=snapshots[path], agent_root=project_root)
                    # These schema/path checks depend on effective lower layers.
                    hook_plan = validate_effective_definition(snapshots[path], project_root, str(path))
                    AgentConfigNormalizer.validate_agent_runtime_config(
                        config, effective_config=snapshots[path].values, hook_plan=hook_plan,
                    )
                except (TypeError, ValueError, OSError, yaml.YAMLError) as exc:
                    errors.append(f"{path}: {definition_error(exc)}")
            raw_workers = config.get("worker_agents", [])
            try:
                AgentConfigNormalizer.validate_worker_agents_config(raw_workers)
            except ValueError:
                return  # Reported by the schema validator above.
            for item in raw_workers:
                reference = item["path"]
                try:
                    candidate = resolve_worker_path(project_root, path, reference)
                    if candidate.suffix.lower() not in {".yaml", ".yml", ".md"}:
                        raise ValueError(f"unsupported extension: {candidate.suffix}")
                    try:
                        relative = candidate.relative_to(project_root).as_posix()
                    except ValueError:
                        relative = None
                    if relative in draft_paths:
                        child = draft_configs.get(relative)
                        if child is None:
                            continue
                    else:
                        read = read_agent_definition(candidate, cache=cache)
                        if read.error or read.definition is None:
                            raise ValueError(read.error or "Agent configuration must be a mapping")
                        child = read.definition
                    visit(candidate, child, worker=True)
                except (TypeError, ValueError, OSError, yaml.YAMLError) as exc:
                    errors.append(f"{path}: worker_agents path '{reference}' is invalid: {definition_error(exc)}")
        finally:
            errors_by_path[path] = tuple(errors[error_start:])
            active.pop()

    visit(source_path, parsed, worker=root_is_worker)
    return ApplicationDefinitionInspection(nodes, snapshots, tuple(errors), errors_by_path)


def validate_effective_definition(snapshot: EffectiveAgentConfigSnapshot, root: Path, source: str):
    from agentloom.application.validation import build_normalized_execution_config
    from agentloom.runtime.hooks.config import HookConfigLayer, HookPlanCompiler

    # Keep the parsed catalog available for inspection even if another
    # capability (for example a tool reference) makes the definition invalid.
    skill_catalog(snapshot)
    # Compilation builds an immutable plan only; handlers are never invoked.
    hook_plan = HookPlanCompiler().compile(
        tuple(
            HookConfigLayer(layer.name, layer.data, layer.root, layer.source_path, priority)
            for priority, layer in enumerate(snapshot.layers)
        )
    )
    normalized = build_normalized_execution_config(snapshot.values, source_name=source, agent_root=root)
    if normalized.prompt_template_path and not Path(normalized.prompt_template_path).is_file():
        raise ValueError(f"Prompt template does not exist: {normalized.prompt_template_path}")
    from agentloom.adapters.mcp.config import parse_mcp_yaml_value

    snapshot.values["_mcp_settings_snapshot"] = parse_mcp_yaml_value(
        snapshot.values.get("mcp_servers"),
        root,
        strict=True,
    )
    AgentConfigNormalizer.validate_runtime_tool_references(snapshot.values)

    return hook_plan


def inspect_application_definition(
    project_root: Path,
    relative_path: str,
    parsed: dict[str, object],
    *,
    root_is_worker: bool = False,
    draft_paths: set[str] | None = None,
    draft_configs: Mapping[str, dict[str, object]] | None = None,
    catalog: tuple[str, dict[str, object]] | None = None,
    definition_cache: AgentDefinitionCache | None = None,
    base_config: UnifiedConfig | None = None,
) -> ApplicationDefinitionInspection:
    """Read and validate one complete topology without constructing a runtime."""
    root = project_root.resolve()
    errors = []
    base = base_config
    if base is None:
        try:
            base = load_project_config(root)
        except (TypeError, ValueError, OSError, yaml.YAMLError) as exc:
            errors.append(f"{root}/config: {definition_error(exc)}")
    inspection = _walk_definitions(
        root,
        root / relative_path,
        parsed,
        root_is_worker=root_is_worker,
        catalog=catalog or (model_catalog(base) if base is not None else model_types(root)),
        base=base,
        draft_paths=draft_paths or set(),
        draft_configs=draft_configs or {},
        cache=definition_cache if definition_cache is not None else {},
    )
    return ApplicationDefinitionInspection(
        inspection.definitions,
        inspection.snapshots,
        tuple(dict.fromkeys([*errors, *inspection.errors])),
        inspection.errors_by_path,
    )


def validate_agent_definition(
    project_root: Path,
    relative_path: str,
    parsed: dict[str, object],
    *,
    root_is_worker: bool = False,
    draft_paths: set[str] | None = None,
    draft_configs: Mapping[str, dict[str, object]] | None = None,
    catalog: tuple[str, dict[str, object]] | None = None,
    definition_cache: AgentDefinitionCache | None = None,
    base_config: UnifiedConfig | None = None,
) -> list[str]:
    return list(inspect_application_definition(
        project_root,
        relative_path,
        parsed,
        root_is_worker=root_is_worker,
        draft_paths=draft_paths,
        draft_configs=draft_configs,
        catalog=catalog,
        definition_cache=definition_cache,
        base_config=base_config,
    ).errors)


def prepare_application_definition(
    project_root: Path,
    source_path: Path,
    parsed: dict[str, object],
    *,
    base_config: UnifiedConfig,
) -> dict[str, object]:
    """Validate and pin every referenced definition/config before Run allocation."""
    inspection = inspect_application_definition(
        project_root,
        str(source_path),
        parsed,
        base_config=base_config,
    )
    if inspection.errors:
        raise ValueError("\n".join(inspection.errors))
    nodes, snapshots = inspection.definitions, inspection.snapshots
    for path, config in nodes.items():
        config["_effective_agent_config_snapshot"] = snapshots[path]
        config["_skill_catalog_snapshot"] = skill_catalog(snapshots[path])
        config["_worker_definitions"] = {
            str(resolve_worker_path(project_root, path, item["path"])): nodes[
                resolve_worker_path(project_root, path, item["path"])
            ]
            for item in config.get("worker_agents", [])
        }
    return copy.deepcopy(nodes[source_path.resolve()])


def skill_catalog(snapshot: EffectiveAgentConfigSnapshot, *, logger=None) -> SkillCatalog:
    """Parse Skill instructions once for this definition's effective sources."""
    from agentloom.runtime.skills.catalog import SkillCatalog

    catalog = snapshot.values.get("_skill_catalog_snapshot")
    if catalog is None:
        catalog = SkillCatalog.discover(skill_sources(snapshot), logger=logger)
        snapshot.values["_skill_catalog_snapshot"] = catalog
    return catalog


def skill_sources(snapshot: EffectiveAgentConfigSnapshot):
    """Resolve Skill roots with the same layer and path rules for every adapter."""
    from agentloom.runtime.skills.catalog import SkillSource

    sources = []
    for layer in snapshot.layers:
        scope = {"global_system": "project", "application_system": "application", "agent": "agent"}.get(layer.name)
        if scope is None:
            continue
        if scope in {"project", "application"}:
            sources.append(SkillSource(path=layer.root / "skills", scope=scope))
        configured = layer.data.get("skills")
        if configured is None:
            continue
        AgentConfigNormalizer.validate_skills_config(layer.data)
        for raw_path in configured["paths"]:
            path = Path(raw_path).expanduser()
            sources.append(SkillSource(path=path if path.is_absolute() else layer.root / path, scope=scope))
    return tuple(sources)
