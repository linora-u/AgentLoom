"""Shared, side-effect-free Application definition and topology semantics.

Only data is read here. Model construction, tool imports, MCP connections, and
Hook execution belong to the execution adapter after preflight succeeds.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from agentloom.app.readiness import (
    validate_runtime_agent_config,
    validate_runtime_worker_config,
)
from agentloom.app.validation import AgentConfigNormalizer
from agentloom.config.config import (
    EffectiveAgentConfigSnapshot,
    UnifiedConfig,
    build_effective_agent_config_snapshot,
    load_project_config,
)
from agentloom.config.llm_config import LLMConfig
from agentloom.config.yaml_loader import load_unique_yaml
from pydantic import ValidationError

if TYPE_CHECKING:
    from agentloom.execution.skills.catalog import SkillCatalog

_AGENT_DEFINITION_EXTENSIONS = frozenset({".yaml", ".yml"})
_AGENT_DEFINITION_MAX_BYTES = 1024 * 1024


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


@dataclass(frozen=True)
class _SnapshotFile:
    payload: bytes
    metadata: os.stat_result
    fd: int


class _DefinitionSnapshotSession:
    """Read one Application topology through a stable project directory tree."""

    def __init__(self, root: Path):
        self.root = root
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(root, directory_flags)
        self._directory_flags = directory_flags
        self._directory_fds: dict[tuple[str, ...], int] = {(): root_fd}
        self._directory_stats: dict[tuple[str, ...], os.stat_result] = {(): os.fstat(root_fd)}
        self._files: dict[Path, _SnapshotFile] = {}
        self._missing_paths: set[Path] = set()
        self._missing_entries: list[tuple[tuple[str, ...], str]] = []

    def __enter__(self) -> _DefinitionSnapshotSession:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for source in reversed(tuple(self._files.values())):
            os.close(source.fd)
        for fd in reversed(tuple(self._directory_fds.values())):
            os.close(fd)

    @staticmethod
    def _validate_relative(relative: Path) -> None:
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Application snapshot path must stay inside the project")

    def _directory_fd(self, parts: tuple[str, ...]) -> int:
        current_parts: tuple[str, ...] = ()
        current_fd = self._directory_fds[current_parts]
        for part in parts:
            next_parts = (*current_parts, part)
            cached = self._directory_fds.get(next_parts)
            if cached is None:
                cached = os.open(
                    part,
                    self._directory_flags,
                    dir_fd=current_fd,
                )
                self._directory_fds[next_parts] = cached
                self._directory_stats[next_parts] = os.fstat(cached)
            current_parts = next_parts
            current_fd = cached
        return current_fd

    def _optional_directory_fd(self, parts: tuple[str, ...]) -> int | None:
        current_parts: tuple[str, ...] = ()
        current_fd = self._directory_fds[current_parts]
        for part in parts:
            next_parts = (*current_parts, part)
            cached = self._directory_fds.get(next_parts)
            if cached is None:
                try:
                    cached = os.open(
                        part,
                        self._directory_flags,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    self._missing_entries.append((current_parts, part))
                    return None
                self._directory_fds[next_parts] = cached
                self._directory_stats[next_parts] = os.fstat(cached)
            current_parts = next_parts
            current_fd = cached
        return current_fd

    def read(
        self,
        relative: Path,
        *,
        optional: bool = False,
    ) -> tuple[Path, bytes, os.stat_result] | None:
        self._validate_relative(relative)
        if relative in self._missing_paths:
            return None
        if relative in self._files:
            cached = self._files[relative]
            return self.root / relative, cached.payload, cached.metadata
        parent_fd = (
            self._optional_directory_fd(relative.parts[:-1])
            if optional
            else self._directory_fd(relative.parts[:-1])
        )
        if parent_fd is None:
            self._missing_paths.add(relative)
            return None
        try:
            file_fd = os.open(
                relative.name,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            if optional:
                self._missing_paths.add(relative)
                self._missing_entries.append((relative.parts[:-1], relative.name))
                return None
            raise
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Application snapshot target must be a regular file")
            os.set_blocking(file_fd, True)
            chunks: list[bytes] = []
            remaining = _AGENT_DEFINITION_MAX_BYTES + 1
            while True:
                chunk = os.read(file_fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
                if remaining == 0:
                    break
            after = os.fstat(file_fd)
        except BaseException:
            os.close(file_fd)
            raise
        if not _same_file_snapshot(before, after):
            os.close(file_fd)
            raise ValueError("Application snapshot target changed while it was read")
        payload = b"".join(chunks)
        if len(payload) > _AGENT_DEFINITION_MAX_BYTES:
            os.close(file_fd)
            raise ValueError("Application snapshot target exceeds the size limit")
        self._files[relative] = _SnapshotFile(payload, after, file_fd)
        return self.root / relative, payload, after

    def read_config(self, path: Path) -> dict[str, object]:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Application config must stay inside the project") from exc
        snapshot = self.read(relative, optional=True)
        if snapshot is None:
            return {}
        source_path, payload, _metadata = snapshot
        try:
            loaded = load_unique_yaml(payload.decode("utf-8")) or {}
        except UnicodeError as exc:
            raise ValueError(f"Configuration must be valid UTF-8: {source_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Configuration file must contain a mapping: {source_path}")
        return loaded

    def verify(self) -> None:
        for parent_parts, name in reversed(self._missing_entries):
            parent_fd = self._directory_fds[parent_parts]
            try:
                os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            raise ValueError("Application path appeared during inspection")
        for relative, source in reversed(tuple(self._files.items())):
            parent_fd = self._directory_fds[relative.parts[:-1]]
            descriptor_metadata = os.fstat(source.fd)
            entry_metadata = os.stat(
                relative.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if not _same_file_snapshot(source.metadata, descriptor_metadata) or not _same_file_snapshot(
                source.metadata,
                entry_metadata,
            ):
                raise ValueError("Application file changed during inspection")
        for parts, expected in reversed(tuple(self._directory_stats.items())):
            descriptor_metadata = os.fstat(self._directory_fds[parts])
            if not _same_file_snapshot(expected, descriptor_metadata):
                raise ValueError("Application directory changed during inspection")
            if parts:
                parent_fd = self._directory_fds[parts[:-1]]
                entry_metadata = os.stat(
                    parts[-1],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not _same_file_snapshot(expected, entry_metadata):
                    raise ValueError("Application directory changed during inspection")
            else:
                root_metadata = os.stat(self.root, follow_symlinks=False)
                if not _same_file_snapshot(expected, root_metadata):
                    raise ValueError("Application project root changed during inspection")

    def revision(self) -> str:
        digest = hashlib.sha256()
        for relative in sorted(self._files, key=lambda item: item.as_posix()):
            source = self._files[relative]
            name = relative.as_posix().encode("utf-8")
            digest.update(len(name).to_bytes(4, "big"))
            digest.update(name)
            digest.update(len(source.payload).to_bytes(8, "big"))
            digest.update(source.payload)
        for relative in sorted(self._missing_paths, key=lambda item: item.as_posix()):
            name = relative.as_posix().encode("utf-8")
            digest.update(b"\x00")
            digest.update(len(name).to_bytes(4, "big"))
            digest.update(name)
        return f"sha256:{digest.hexdigest()}"


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


@dataclass(frozen=True)
class SupervisorDefinitionInspection:
    """One canonical project Supervisor definition and its validation result."""

    path: Path
    relative_path: str
    application_id: str
    definition_snapshot_revision: str
    definition: dict[str, object]
    prepared_definition: dict[str, object] | None
    errors: tuple[str, ...]


def _read_definition_snapshot(
    root: Path,
    relative: Path,
    *,
    snapshot_session: _DefinitionSnapshotSession | None = None,
) -> tuple[Path, bytes, os.stat_result]:
    if snapshot_session is not None:
        snapshot = snapshot_session.read(relative)
        assert snapshot is not None
        return snapshot
    with _DefinitionSnapshotSession(root) as session:
        snapshot = session.read(relative)
        assert snapshot is not None
        session.verify()
        return snapshot


def _definition_from_bytes(path: Path, payload: bytes) -> dict[str, object]:
    try:
        content = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"Agent definition must be valid UTF-8: {path}") from exc
    if path.suffix.lower() in {".yaml", ".yml"}:
        raw = load_unique_yaml(content)
    else:
        raise ValueError(f"Unsupported file format: {path.suffix}")
    if not isinstance(raw, dict):
        raise ValueError("Agent configuration must be a mapping")
    prepared = copy.deepcopy(raw)
    prepared["_yaml_file_path"] = str(path)
    return prepared


def _resolve_system_prompt(
    config: dict[str, object],
    *,
    path: Path,
    project_root: Path,
    snapshot_session: _DefinitionSnapshotSession | None,
) -> None:
    raw = config.get("system_prompt")
    if raw is None:
        return
    if isinstance(raw, str):
        if not raw.strip():
            raise ValueError("system_prompt must be non-empty when provided")
        config["_resolved_system_prompt"] = raw
        return
    if not isinstance(raw, dict) or set(raw) != {"path"}:
        raise ValueError("system_prompt must be a string or a mapping containing only path")
    source = raw["path"]
    if not isinstance(source, str) or not source.strip() or Path(source).is_absolute():
        raise ValueError("system_prompt.path must be a non-empty relative path")
    target = Path(os.path.normpath(path.parent / source))
    try:
        relative = target.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("system_prompt.path must stay inside the project") from exc
    if snapshot_session is not None:
        source_snapshot = snapshot_session.read(relative)
        assert source_snapshot is not None
        payload = source_snapshot[1]
    else:
        payload = target.read_bytes()
    if len(payload) > _AGENT_DEFINITION_MAX_BYTES:
        raise ValueError("system_prompt.path content is too large")
    try:
        content = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("system_prompt.path must contain UTF-8 text") from exc
    if not content.strip():
        raise ValueError("system_prompt.path content must be non-empty")
    config["_resolved_system_prompt"] = content
    config["_system_prompt_source"] = str(target)


def prepare_agent_system_prompt(
    config: dict[str, object],
    *,
    source_path: Path | None,
    project_root: Path | None = None,
    source_path_is_pinned: bool = False,
) -> None:
    """Resolve instructions for direct factory callers using inspection semantics."""

    if "system_prompt" not in config:
        return
    if source_path_is_pinned and isinstance(config.get("_resolved_system_prompt"), str):
        return
    raw = config["system_prompt"]
    if isinstance(raw, dict) and source_path is None:
        raise ValueError("system_prompt.path requires a source YAML path")
    if source_path is None:
        _resolve_system_prompt(
            config, path=Path("."), project_root=Path("."), snapshot_session=None
        )
        return
    root = project_root or source_path.parent
    with _DefinitionSnapshotSession(root) as session:
        _resolve_system_prompt(
            config, path=source_path, project_root=root, snapshot_session=session
        )
        session.verify()


def load_prepared_agent_definition(
    path: Path | str, *, project_root: Path | None = None
) -> dict[str, object]:
    """Read a direct YAML definition and referenced instructions as one snapshot."""

    source_path = Path(path).expanduser().absolute()
    root = project_root or source_path.parent
    relative = source_path.relative_to(root)
    with _DefinitionSnapshotSession(root) as session:
        source = session.read(relative)
        assert source is not None
        prepared = _definition_from_bytes(source_path, source[1])
        _resolve_system_prompt(
            prepared, path=source_path, project_root=root, snapshot_session=session
        )
        session.verify()
    return prepared


def inspect_supervisor_definition(
    project_root: Path | str,
    configured_path: str,
    *,
    catalog: tuple[str, dict[str, object]] | None = None,
    definition_cache: AgentDefinitionCache | None = None,
    base_config: UnifiedConfig | None = None,
) -> SupervisorDefinitionInspection:
    """Resolve and validate one canonical, non-symlink Supervisor definition."""

    root = Path(project_root).expanduser().resolve()
    if (
        not isinstance(configured_path, str)
        or not configured_path
        or configured_path != configured_path.strip()
        or "\\" in configured_path
    ):
        raise ValueError("Supervisor path must be a canonical project-relative path")
    relative = Path(configured_path)
    parts = relative.parts
    try:
        workflow_index = parts.index("workflows")
    except ValueError:
        workflow_index = -1
    application_id = "/".join(parts[1:workflow_index])
    if (
        relative.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] != "applications"
        or workflow_index < 2
        or workflow_index >= len(parts) - 1
        or "worker_agents" in parts[workflow_index + 1 : -1]
        or relative.suffix.lower() not in _AGENT_DEFINITION_EXTENSIONS
    ):
        raise ValueError("Path must identify a project Supervisor definition")
    try:
        with _DefinitionSnapshotSession(root) as snapshot_session:
            resolved, payload, _metadata = _read_definition_snapshot(
                root,
                relative,
                snapshot_session=snapshot_session,
            )
            canonical = relative.as_posix()
            definition = _definition_from_bytes(resolved, payload)
            if canonical != configured_path:
                raise ValueError("Supervisor path is not canonical")

            cache = definition_cache if definition_cache is not None else {}
            cache[resolved] = AgentDefinitionRead(
                copy.deepcopy(definition),
                None,
            )
            application_inspection = inspect_application_definition(
                root,
                canonical,
                definition,
                catalog=(catalog or (model_catalog(base_config) if base_config is not None else model_types(root))),
                definition_cache=cache,
                base_config=base_config,
                root_path_is_pinned=True,
                snapshot_session=snapshot_session,
            )
            errors = list(application_inspection.errors)
            prepared_definition = (
                None
                if errors
                else _prepare_inspected_definition(
                    root,
                    resolved,
                    application_inspection,
                    paths_are_pinned=True,
                )
            )
            if prepared_definition is not None:
                prepared_definition["_application_id"] = application_id
            snapshot_session.verify()
            definition_snapshot_revision = snapshot_session.revision()
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError("Path must identify a real project Supervisor definition") from exc
    return SupervisorDefinitionInspection(
        path=resolved,
        relative_path=canonical,
        application_id=application_id,
        definition_snapshot_revision=definition_snapshot_revision,
        definition=definition,
        prepared_definition=prepared_definition,
        errors=tuple(errors),
    )


def resolve_valid_supervisor_definition(
    project_root: Path | str,
    configured_path: str | Path,
) -> Path:
    """Return one canonical valid Supervisor definition for an Application action."""

    raw_path = str(configured_path)
    try:
        inspection = inspect_supervisor_definition(project_root, raw_path)
    except ValueError as exc:
        raise ValueError("yaml_path must identify a real, non-symlink project Supervisor Agent definition") from exc
    if inspection.errors:
        raise ValueError("yaml_path must identify a valid supervisor Agent definition")
    return inspection.path


def discover_application_definition_files(
    workflows_dir: Path | str,
) -> tuple[DiscoveredAgentDefinition, ...]:
    """Recursively find YAML definitions without following symlinks.

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


def load_agent_definition(path: Path | str) -> dict[str, object]:
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
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
    root_path_is_pinned: bool = False,
    snapshot_session: _DefinitionSnapshotSession | None = None,
) -> ApplicationDefinitionInspection:
    from agentloom.app.paths import worker_reference_candidate

    nodes: dict[Path, dict[str, object]] = {}
    snapshots: dict[Path, EffectiveAgentConfigSnapshot] = {}
    errors: list[str] = []
    errors_by_path: dict[Path, tuple[str, ...]] = {}
    active: list[Path] = []

    def visit(
        path: Path,
        config: dict[str, object],
        *,
        worker: bool,
        path_is_pinned: bool = False,
    ) -> None:
        if not path_is_pinned:
            path = path.resolve()
        if path in active:
            errors.append("Worker reference cycle: " + " -> ".join(str(p) for p in [*active, path]))
            return
        if path in nodes:
            return
        config = copy.deepcopy(config)
        config.pop("_application_id", None)
        config["_yaml_file_path"] = str(path)
        nodes[path] = config
        active.append(path)
        error_start = len(errors)
        try:
            try:
                _resolve_system_prompt(
                    config,
                    path=path,
                    project_root=project_root,
                    snapshot_session=snapshot_session,
                )
            except (OSError, UnicodeError, TypeError, ValueError) as exc:
                errors.append(f"{path}: {definition_error(exc)}")
            validator = validate_runtime_worker_config if worker else validate_runtime_agent_config
            try:
                validator(config, path, agent_root=project_root)
            except (TypeError, ValueError) as exc:
                errors.append(f"{path}: {definition_error(exc)}")
            errors.extend(_validate_model_reference(path, config, catalog))
            if base is not None:
                try:
                    application_config = None
                    if path_is_pinned:
                        if snapshot_session is None:
                            raise ValueError("Pinned Application inspection requires a snapshot session")
                        current = path.parent
                        while current != project_root and current.name != "workflows":
                            current = current.parent
                        if current.name != "workflows":
                            raise ValueError("Cannot locate Application workflows directory")
                        application_config = snapshot_session.read_config(
                            current.parent / "config" / "system.yaml"
                        )
                    snapshots[path] = build_effective_agent_config_snapshot(
                        config,
                        source_name=str(path),
                        base_config=base,
                        source_path_is_pinned=path_is_pinned,
                        application_config=application_config,
                    )
                    AgentConfigNormalizer.validate_agent_runtime_config(
                        config,
                        effective_config=snapshots[path].values,
                    )
                    from agentloom.app.runtime_options import normalize_runtime_options

                    runtime_options, _ = normalize_runtime_options(
                        config, snapshot=snapshots[path], agent_root=project_root
                    )
                    # These schema/path checks depend on effective lower layers.
                    hook_plan = validate_effective_definition(
                        snapshots[path],
                        project_root,
                        str(path),
                        runtime_id=config.get("agent_runtime"),
                        runtime_options=runtime_options,
                    )
                    AgentConfigNormalizer.validate_agent_runtime_config(
                        config,
                        effective_config=snapshots[path].values,
                        hook_plan=hook_plan,
                    )
                except (TypeError, ValueError, OSError, yaml.YAMLError) as exc:
                    errors.append(f"{path}: {definition_error(exc)}")
            raw_workers = config.get("worker_agents", [])
            try:
                AgentConfigNormalizer.validate_worker_agents_config(raw_workers)
            except ValueError:
                return  # Reported by the schema validator above.
            for item in cast(list[dict[str, Any]], raw_workers):
                reference = item["path"]
                try:
                    if root_path_is_pinned:
                        candidate = worker_reference_candidate(
                            reference,
                            path.parent / "worker_agents",
                            project_root=project_root,
                        )
                        candidate = Path(os.path.normpath(candidate))
                    else:
                        candidate = resolve_worker_path(
                            project_root,
                            path,
                            reference,
                        )
                    if candidate.suffix.lower() not in {".yaml", ".yml"}:
                        raise ValueError(f"unsupported extension: {candidate.suffix}")
                    try:
                        relative = candidate.relative_to(project_root).as_posix()
                    except ValueError:
                        relative = None
                    if relative in draft_paths:
                        child = draft_configs.get(relative)
                        if child is None:
                            continue
                    elif root_path_is_pinned:
                        if relative is None:
                            raise ValueError("scheduled Supervisor Worker must stay inside the project")
                        worker_path, payload, _metadata = _read_definition_snapshot(
                            project_root,
                            Path(relative),
                            snapshot_session=snapshot_session,
                        )
                        child = _definition_from_bytes(worker_path, payload)
                        candidate = worker_path
                        cache[candidate] = AgentDefinitionRead(
                            copy.deepcopy(child),
                            None,
                        )
                    else:
                        read = read_agent_definition(candidate, cache=cache)
                        if read.error or read.definition is None:
                            raise ValueError(read.error or "Agent configuration must be a mapping")
                        child = read.definition
                    visit(
                        candidate,
                        child,
                        worker=True,
                        path_is_pinned=root_path_is_pinned,
                    )
                except (TypeError, ValueError, OSError, yaml.YAMLError) as exc:
                    errors.append(f"{path}: worker_agents path '{reference}' is invalid: {definition_error(exc)}")
        finally:
            errors_by_path[path] = tuple(errors[error_start:])
            active.pop()

    visit(
        source_path,
        parsed,
        worker=root_is_worker,
        path_is_pinned=root_path_is_pinned,
    )
    return ApplicationDefinitionInspection(nodes, snapshots, tuple(errors), errors_by_path)


def validate_effective_definition(
    snapshot: EffectiveAgentConfigSnapshot, root: Path, source: str, *,
    runtime_id: object = "smolagents", runtime_options: dict | None = None,
):
    from agentloom.execution.hooks.config import HookConfigLayer, HookPlanCompiler

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
    if runtime_id == "smolagents":
        prompt_path = (
            runtime_options.get("prompt_template_path") if runtime_options is not None
            else None
        )
        if prompt_path and not Path(prompt_path).is_file():
            raise ValueError(f"Prompt template does not exist: {prompt_path}")
    from agentloom.integrations.mcp.config import parse_mcp_yaml_value

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
    root_path_is_pinned: bool = False,
    snapshot_session: _DefinitionSnapshotSession | None = None,
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
        root_path_is_pinned=root_path_is_pinned,
        snapshot_session=snapshot_session,
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
    return _prepare_inspected_definition(
        project_root,
        source_path,
        inspection,
    )


def _prepare_inspected_definition(
    project_root: Path,
    source_path: Path,
    inspection: ApplicationDefinitionInspection,
    *,
    paths_are_pinned: bool = False,
) -> dict[str, object]:
    """Build the execution graph from one already completed inspection."""

    nodes, snapshots = inspection.definitions, inspection.snapshots
    for path, config in nodes.items():
        config["_effective_agent_config_snapshot"] = snapshots[path]
        config["_skill_catalog_snapshot"] = skill_catalog(snapshots[path])
        workers: dict[str, dict[str, object]] = {}
        worker_paths: dict[str, str] = {}
        for item in cast(list[dict[str, Any]], config.get("worker_agents", [])):
            from agentloom.app.paths import worker_reference_candidate

            candidate = worker_reference_candidate(
                item["path"],
                path.parent / "worker_agents",
                project_root=project_root,
            )
            candidate = Path(os.path.normpath(candidate))
            if paths_are_pinned:
                if candidate not in nodes:
                    raise ValueError(
                        f"Prepared Application snapshot is missing Worker: {item['path']}"
                    )
                worker_key = candidate
            else:
                worker_key = candidate if candidate in nodes else candidate.resolve()
            workers[str(worker_key)] = nodes[worker_key]
            worker_paths[item["path"]] = str(worker_key)
        config["_worker_definitions"] = workers
        config["_worker_definition_paths"] = worker_paths
    if paths_are_pinned:
        if source_path not in nodes:
            raise ValueError("Prepared Application snapshot is missing its Supervisor")
        source_key = source_path
    else:
        source_key = source_path if source_path in nodes else source_path.resolve()
    return copy.deepcopy(nodes[source_key])


def skill_catalog(snapshot: EffectiveAgentConfigSnapshot, *, logger=None) -> SkillCatalog:
    """Parse Skill instructions once for this definition's effective sources."""
    from agentloom.execution.skills.catalog import SkillCatalog

    catalog = snapshot.values.get("_skill_catalog_snapshot")
    if catalog is None:
        catalog = SkillCatalog.discover(skill_sources(snapshot), logger=logger)
        snapshot.values["_skill_catalog_snapshot"] = catalog
    return catalog


def skill_sources(snapshot: EffectiveAgentConfigSnapshot):
    """Resolve Skill roots with the same layer and path rules for every adapter."""
    from agentloom.execution.skills.catalog import SkillSource

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
