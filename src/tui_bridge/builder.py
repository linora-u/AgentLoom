"""Bounded Agent YAML builder used by the AgentLoom TUI.

The model can inspect Agent definitions and edit an in-memory draft.  It cannot
run commands, execute an Agent, or write files.  Applying a validated draft is
an explicit RPC operation performed by the user.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

import yaml
from smolagents import Tool

from src.lib.smolagents.agent.agent_validation import AgentConfigNormalizer
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
from src.runner import validate_runtime_agent_config, validate_runtime_worker_config

_MAX_TRANSCRIPT_MESSAGES = 16
_MAX_BUILDER_MESSAGE_CHARS = 32_000
_MAX_TRANSCRIPT_CHARS = 64_000
_MAX_ASSISTANT_MESSAGE_CHARS = 32_000
_MAX_INSPECTION_BYTES = 80_000
_TRUNCATION_MARKER = "… [truncated]"


class DraftConflictError(ValueError):
    """Raised when the UI tries to apply a stale draft revision."""


class _BuilderAgent(Protocol):
    def run(self, prompt: str) -> object: ...


AgentFactory = Callable[[Sequence[Tool], str | None], _BuilderAgent]


def _bounded_assistant_message(content: str) -> str:
    if len(content) <= _MAX_ASSISTANT_MESSAGE_CHARS:
        return content
    return content[: _MAX_ASSISTANT_MESSAGE_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _trim_history(history: list[dict[str, str]]) -> None:
    def exceeds_budget() -> bool:
        return (
            len(history) > _MAX_TRANSCRIPT_MESSAGES
            or sum(len(item["content"]) for item in history) > _MAX_TRANSCRIPT_CHARS
        )

    while len(history) > 1 and exceeds_budget():
        # Normal history is user/assistant pairs. Remove a whole old turn so
        # the prompt does not begin with a detached assistant reply.
        remove_count = 2 if history[0].get("role") == "user" and history[1].get("role") == "assistant" else 1
        del history[:remove_count]


@dataclass
class _Draft:
    files: dict[str, str] = field(default_factory=dict)
    base_fingerprints: dict[str, _FileFingerprint | None] = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class _FileFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    digest: str


@dataclass
class _PreparedWrite:
    relative_path: str
    parent_fd: int
    target_name: str
    temporary_name: str
    backup_name: str
    target_path: Path
    backup_path: Path
    existed: bool
    original_fingerprint: _FileFingerprint | None = None
    written_fingerprint: _FileFingerprint | None = None
    preserve_backup: bool = False


def _normalize_agent_yaml_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    candidate = PurePosixPath(normalized)
    parts = candidate.parts
    valid = (
        bool(normalized)
        and not candidate.is_absolute()
        and ".." not in parts
        and len(parts) >= 4
        and parts[0] == "applications"
        and "workflows" in parts[2:]
        and candidate.suffix.lower() in {".yaml", ".yml"}
    )
    if not valid:
        raise ValueError("Agent YAML path must match applications/<application>/.../workflows/<agent>.yaml")
    workflow_index = parts.index("workflows")
    if workflow_index == len(parts) - 1:
        raise ValueError("Agent YAML path must match applications/<application>/.../workflows/<agent>.yaml")
    return candidate.as_posix()


def _target_path(project_root: Path, relative_path: str) -> Path:
    lexical = project_root.joinpath(*PurePosixPath(relative_path).parts)
    applications_root = project_root / "applications"
    _reject_symlink_components(project_root, lexical)
    target = lexical.resolve(strict=False)
    if not target.is_relative_to(applications_root.resolve(strict=False)):
        raise ValueError("Agent YAML path escapes the project applications directory")
    return lexical


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Agent YAML path escapes the project root") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Agent YAML path contains a symlink: {current}")


def _open_flags(base: int) -> int:
    return base | getattr(os, "O_NOFOLLOW", 0)


def _directory_flags() -> int:
    return _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))


def _open_relative_directory(root_fd: int, parts: Sequence[str], *, create: bool) -> int:
    """Open a directory below ``root_fd`` without following replaceable parents."""

    directory_fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise ValueError("Agent YAML parent escapes the project root")
            try:
                child_fd = os.open(part, _directory_flags(), dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, _directory_flags(), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _stable_file_metadata(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IMODE(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _fingerprint_descriptor(descriptor: int, name: str) -> _FileFingerprint:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Agent YAML target is not a regular file: {name}")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    after = os.fstat(descriptor)
    if _stable_file_metadata(before) != _stable_file_metadata(after) or size != after.st_size:
        raise DraftConflictError(f"Agent YAML target changed while being fingerprinted: {name}")
    return _FileFingerprint(
        device=after.st_dev,
        inode=after.st_ino,
        mode=stat.S_IMODE(after.st_mode),
        size=after.st_size,
        modified_ns=after.st_mtime_ns,
        digest=digest.hexdigest(),
    )


def _fingerprint_entry(directory_fd: int, name: str) -> _FileFingerprint | None:
    try:
        descriptor = os.open(
            name,
            _open_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    try:
        return _fingerprint_descriptor(descriptor, name)
    finally:
        os.close(descriptor)


def _fingerprint_project_entry(project_root: Path, relative_path: str) -> _FileFingerprint | None:
    parts = PurePosixPath(relative_path).parts
    project_fd = os.open(project_root, _directory_flags())
    parent_fd: int | None = None
    try:
        try:
            parent_fd = _open_relative_directory(project_fd, parts[:-1], create=False)
        except FileNotFoundError:
            return None
        return _fingerprint_entry(parent_fd, parts[-1])
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(project_fd)


def _write_exclusive(
    directory_fd: int,
    name: str,
    content: str,
    *,
    mode: int | None = None,
) -> _FileFingerprint:
    descriptor = os.open(
        name,
        _open_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)),
        mode if mode is not None else 0o666,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            if mode is not None:
                os.fchmod(descriptor, mode)
            os.fsync(stream.fileno())
        return _fingerprint_descriptor(descriptor, name)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while preparing Agent YAML backup")
        offset += written


def _backup_file(directory_fd: int, source_name: str, backup_name: str) -> _FileFingerprint:
    source_descriptor = os.open(
        source_name,
        _open_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)),
        dir_fd=directory_fd,
    )
    backup_descriptor: int | None = None
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Agent YAML target is not a regular file: {source_name}")
        backup_descriptor = os.open(
            backup_name,
            _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)),
            stat.S_IMODE(before.st_mode),
            dir_fd=directory_fd,
        )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(backup_descriptor, chunk)
        os.fchmod(backup_descriptor, stat.S_IMODE(before.st_mode))
        os.fsync(backup_descriptor)
        after = os.fstat(source_descriptor)
        if _stable_file_metadata(before) != _stable_file_metadata(after) or size != after.st_size:
            raise DraftConflictError(f"Agent YAML target changed while being backed up: {source_name}")
        return _FileFingerprint(
            device=after.st_dev,
            inode=after.st_ino,
            mode=stat.S_IMODE(after.st_mode),
            size=after.st_size,
            modified_ns=after.st_mtime_ns,
            digest=digest.hexdigest(),
        )
    finally:
        os.close(source_descriptor)
        if backup_descriptor is not None:
            os.close(backup_descriptor)


def _entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _entry_exists(directory_fd: int, name: str) -> bool:
    return _entry_stat(directory_fd, name) is not None


def _unlink_entry(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _assert_prepared_write_unchanged(item: _PreparedWrite) -> None:
    try:
        target_fingerprint = _fingerprint_entry(item.parent_fd, item.target_name)
        temporary_fingerprint = _fingerprint_entry(item.parent_fd, item.temporary_name)
    except (OSError, ValueError) as exc:
        raise DraftConflictError(f"Agent YAML target changed before commit: {item.relative_path}") from exc
    if target_fingerprint != item.original_fingerprint:
        raise DraftConflictError(f"Agent YAML target changed before commit: {item.relative_path}")
    if temporary_fingerprint != item.written_fingerprint:
        raise DraftConflictError(f"Prepared Agent YAML changed before commit: {item.relative_path}")


def _assert_prepared_parent_unchanged(project_fd: int, item: _PreparedWrite) -> None:
    parent_parts = PurePosixPath(item.relative_path).parts[:-1]
    canonical_parent_fd: int | None = None
    try:
        canonical_parent_fd = _open_relative_directory(project_fd, parent_parts, create=False)
        held_parent = os.fstat(item.parent_fd)
        canonical_parent = os.fstat(canonical_parent_fd)
        if (held_parent.st_dev, held_parent.st_ino) != (canonical_parent.st_dev, canonical_parent.st_ino):
            raise DraftConflictError(f"Agent YAML parent changed before commit: {item.relative_path}")
    except DraftConflictError:
        raise
    except (OSError, ValueError) as exc:
        raise DraftConflictError(f"Agent YAML parent changed before commit: {item.relative_path}") from exc
    finally:
        if canonical_parent_fd is not None:
            os.close(canonical_parent_fd)


def _read_bounded_regular_file(root: Path, path: Path, max_bytes: int) -> tuple[str, bool, int]:
    """Read at most ``max_bytes`` without following any path-component symlink."""

    if max_bytes <= 0:
        return "", False, 0
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Agent YAML path escapes the project root") from exc
    if not relative.parts:
        raise ValueError("Agent YAML path must identify a file")

    directory_flags = _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    directory_descriptor = os.open(root, directory_flags)
    file_descriptor: int | None = None
    try:
        for part in relative.parts[:-1]:
            child_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor
        file_descriptor = os.open(
            relative.parts[-1],
            _open_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)),
            dir_fd=directory_descriptor,
        )
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise ValueError(f"Agent YAML is not a regular file: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        os.close(directory_descriptor)

    raw = b"".join(chunks)
    payload = raw[:max_bytes]
    return payload.decode("utf-8", errors="ignore"), len(raw) > max_bytes, len(payload)


def _model_types(project_root: Path) -> tuple[str, dict[str, object]]:
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
    project_root: Path,
    source_path: Path | str,
    parsed: dict[str, object],
) -> list[str]:
    default_model, models = _model_types(project_root)
    selected_model = parsed.get("model_type", default_model)
    if not isinstance(selected_model, str) or not selected_model.strip():
        return [f"{source_path}: no model_type and no configured default model"]
    selected_model = selected_model.strip()
    model_settings = models.get(selected_model)
    if not isinstance(model_settings, dict) or not model_settings.get("model"):
        return [f"{source_path}: model_type '{selected_model}' is not configured"]
    return []


def _validate_worker_references(
    project_root: Path,
    relative_path: str,
    parsed: dict[str, object],
    draft_paths: set[str],
    draft_configs: Mapping[str, dict[str, object]],
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
                # The staged file's own YAML/mapping validation reports the
                # parse error.  Do not fall back to an older on-disk worker.
                continue
            try:
                validate_runtime_worker_config(
                    worker_config,
                    candidate_relative,
                    agent_root=project_root,
                )
                errors.extend(
                    _validate_model_reference(
                        project_root,
                        candidate_relative,
                        worker_config,
                    )
                )
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
            worker_config = YamlAgentFactory._load_config_from_file(candidate)
            validate_runtime_worker_config(
                worker_config,
                candidate,
                agent_root=project_root,
            )
            errors.extend(
                _validate_model_reference(
                    project_root,
                    candidate,
                    worker_config,
                )
            )
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
) -> list[str]:
    """Validate one parsed Agent definition against Builder and runtime contracts."""

    errors: list[str] = []
    try:
        validate_runtime_agent_config(
            parsed,
            relative_path,
            agent_root=project_root,
        )
    except ValueError as exc:
        errors.append(str(exc))
    for key in ("runtime", "logging"):
        if key in parsed:
            errors.append(f"{relative_path}: '{key}' is global-only and is not allowed in Agent YAML")

    errors.extend(_validate_model_reference(project_root, relative_path, parsed))
    errors.extend(
        f"{relative_path}: {error}"
        for error in _validate_worker_references(
            project_root,
            relative_path,
            parsed,
            draft_paths or set(),
            draft_configs or {},
        )
    )
    return errors


def _validate_yaml(
    project_root: Path,
    relative_path: str,
    content: str,
    *,
    draft_paths: set[str],
    draft_configs: Mapping[str, dict[str, object]],
) -> list[str]:
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return [f"{relative_path}: invalid YAML: {exc}"]
    if not isinstance(parsed, dict):
        return [f"{relative_path}: Agent YAML must be a mapping"]
    return validate_agent_definition(
        project_root,
        relative_path,
        parsed,
        draft_paths=draft_paths,
        draft_configs=draft_configs,
    )


def _draft_summary(project_root: Path, draft: _Draft) -> dict[str, object]:
    errors: list[str] = []
    files: list[dict[str, object]] = []
    draft_paths = set(draft.files)
    draft_configs: dict[str, dict[str, object]] = {}
    for relative_path, content in draft.files.items():
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            draft_configs[relative_path] = parsed
    for relative_path, content in sorted(draft.files.items()):
        errors.extend(
            _validate_yaml(
                project_root,
                relative_path,
                content,
                draft_paths=draft_paths,
                draft_configs=draft_configs,
            )
        )
        try:
            target = _target_path(project_root, relative_path)
        except ValueError as exc:
            errors.append(f"{relative_path}: {exc}")
            target = project_root.joinpath(*PurePosixPath(relative_path).parts)
        files.append(
            {
                "path": relative_path,
                "change": "modify" if target.exists() else "create",
                "content": content,
            }
        )
    if not files:
        errors.append("No Agent YAML files are staged")
    return {
        "revision": draft.revision,
        "valid": not errors,
        "errors": errors,
        "files": files,
    }


class _InspectAgentSystemTool(Tool):
    name = "inspect_agent_system"
    description = (
        "Read existing YAML definitions for one Agent System before proposing changes. "
        "The application_id is the path below applications/, for example web_search."
    )
    inputs = {
        "application_id": {
            "type": "string",
            "description": "Application id below applications/; slash-separated ids are allowed.",
        }
    }
    output_type = "string"

    def __init__(self, project_root: Path):
        super().__init__()
        self._project_root = project_root

    def forward(self, application_id: str) -> str:
        application = PurePosixPath(application_id.strip().replace("\\", "/"))
        if application.is_absolute() or ".." in application.parts or not application.parts:
            raise ValueError("application_id must identify a directory below applications/")
        workflows = _target_path(
            self._project_root,
            (PurePosixPath("applications") / application / "workflows").as_posix(),
        )
        if not workflows.is_dir():
            return json.dumps(
                {"application_id": application.as_posix(), "files": [], "exists": False},
                ensure_ascii=False,
            )
        remaining = _MAX_INSPECTION_BYTES
        files: list[dict[str, str]] = []
        for path in sorted((*workflows.rglob("*.yaml"), *workflows.rglob("*.yml"))):
            if path.is_symlink():
                continue
            try:
                content, truncated, consumed = _read_bounded_regular_file(
                    self._project_root,
                    path,
                    remaining,
                )
            except (OSError, ValueError):
                continue
            if truncated:
                content += "\n# … truncated"
            remaining -= consumed
            files.append(
                {
                    "path": path.relative_to(self._project_root).as_posix(),
                    "content": content,
                }
            )
            if remaining <= 0:
                break
        return json.dumps(
            {"application_id": application.as_posix(), "files": files, "exists": True},
            ensure_ascii=False,
        )


class _StageAgentYamlTool(Tool):
    name = "stage_agent_yaml"
    description = (
        "Create or replace one Agent YAML file in the in-memory draft. "
        "This never writes to disk; the user must explicitly apply the draft."
    )
    inputs = {
        "path": {
            "type": "string",
            "description": "Path matching applications/<application>/.../workflows/<agent>.yaml.",
        },
        "content": {"type": "string", "description": "Complete YAML file content."},
    }
    output_type = "string"

    def __init__(self, project_root: Path, draft: _Draft):
        super().__init__()
        self._project_root = project_root
        self._draft = draft

    def forward(self, path: str, content: str) -> str:
        relative_path = _normalize_agent_yaml_path(path)
        _target_path(self._project_root, relative_path)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Agent YAML content must be a non-empty string")
        if relative_path not in self._draft.base_fingerprints:
            self._draft.base_fingerprints[relative_path] = _fingerprint_project_entry(
                self._project_root,
                relative_path,
            )
        if self._draft.files.get(relative_path) != content:
            self._draft.files[relative_path] = content
            self._draft.revision += 1
        return json.dumps(_draft_summary(self._project_root, self._draft), ensure_ascii=False)


class _ValidateAgentDraftTool(Tool):
    name = "validate_agent_draft"
    description = "Validate all staged Agent YAML files and return concrete errors."
    inputs = {}
    output_type = "string"

    def __init__(self, project_root: Path, draft: _Draft):
        super().__init__()
        self._project_root = project_root
        self._draft = draft

    def forward(self) -> str:
        return json.dumps(_draft_summary(self._project_root, self._draft), ensure_ascii=False)


def _default_agent_factory(tools: Sequence[Tool], model_type: str | None) -> _BuilderAgent:
    from src.lib.config import C
    from src.lib.smolagents.agent.base_agent import ToolCallingAgentV2
    from src.lib.smolagents.models.model_manager import get_model

    selected_model = (model_type or C.default_model_type or "").strip()
    model = get_model(selected_model, framework="smolagents")
    return ToolCallingAgentV2(
        tools=list(tools),
        model=model,
        max_steps=4,
        max_tokens=4096,
        verbosity_level=0,
    )


class BuilderService:
    """In-process sessions for the TUI's short Agent Builder conversations."""

    def __init__(self, project_root: Path | str, *, agent_factory: AgentFactory | None = None):
        self._project_root = Path(project_root).expanduser().resolve()
        self._agent_factory = agent_factory or _default_agent_factory
        self._drafts: dict[str, _Draft] = {}
        self._histories: dict[str, list[dict[str, str]]] = {}

    def _draft(self, session_id: str) -> _Draft:
        return self._drafts.setdefault(session_id, _Draft())

    def history(self, session_id: str) -> list[dict[str, str]]:
        return [dict(item) for item in self._histories.get(session_id, [])]

    def get_draft(self, session_id: str) -> dict[str, object]:
        return _draft_summary(self._project_root, self._draft(session_id))

    def send(
        self,
        *,
        session_id: str,
        message: str,
        model_type: str | None = None,
    ) -> dict[str, object]:
        session_id = session_id.strip()
        message = message.strip()
        if not session_id:
            raise ValueError("session_id is required")
        if not message:
            raise ValueError("message is required")
        if len(message) > _MAX_BUILDER_MESSAGE_CHARS:
            raise ValueError(f"message must not exceed {_MAX_BUILDER_MESSAGE_CHARS:,} characters")

        draft = self._draft(session_id)
        history = self._histories.setdefault(session_id, [])
        history.append({"role": "user", "content": message})
        _trim_history(history)

        tools: list[Tool] = [
            _InspectAgentSystemTool(self._project_root),
            _StageAgentYamlTool(self._project_root, draft),
            _ValidateAgentDraftTool(self._project_root, draft),
        ]
        agent = self._agent_factory(tools, model_type)
        transcript = json.dumps(history, ensure_ascii=False)
        prompt = (
            "You are AgentLoom Builder. Help the user design Agent System YAML through a short conversation.\n"
            "You may only inspect existing Agent definitions, stage complete YAML files in memory, and validate the draft.\n"
            "Never claim that you ran an Agent. Never use shell, git, edit arbitrary project files, or perform a long task.\n"
            "Do not claim files were saved: only the user-facing Apply action can write a valid draft.\n"
            "Ask a concise clarification when the requested behavior is materially ambiguous.\n"
            f"Conversation (including the latest user message): {transcript}"
        )
        output = agent.run(prompt)
        assistant = _bounded_assistant_message(str(output))
        history.append({"role": "assistant", "content": assistant})
        _trim_history(history)
        return {
            "session_id": session_id,
            "assistant": assistant,
            "model_type": model_type,
            "draft": _draft_summary(self._project_root, draft),
        }

    def apply_draft(self, *, session_id: str, expected_revision: int) -> dict[str, object]:
        draft = self._draft(session_id)
        if expected_revision != draft.revision:
            raise DraftConflictError(f"Draft revision changed: expected {expected_revision}, current {draft.revision}")
        summary = _draft_summary(self._project_root, draft)
        if not summary["valid"]:
            raise ValueError("Cannot apply an invalid Agent draft: " + "; ".join(summary["errors"]))

        token = f"{os.getpid()}-{uuid.uuid4().hex}"
        prepared: list[_PreparedWrite] = []
        applied: list[str] = []
        project_fd = os.open(self._project_root, _directory_flags())
        try:
            for relative_path, content in sorted(draft.files.items()):
                parts = PurePosixPath(relative_path).parts
                target = self._project_root.joinpath(*parts)
                parent_fd = _open_relative_directory(project_fd, parts[:-1], create=True)
                target_name = parts[-1]
                temporary_name = f".{target_name}.agentloom-{token}.tmp"
                backup_name = f".{target_name}.agentloom-{token}.bak"
                if relative_path not in draft.base_fingerprints:
                    os.close(parent_fd)
                    raise DraftConflictError(f"Agent YAML target has no staged baseline: {relative_path}")
                try:
                    current_fingerprint = _fingerprint_entry(parent_fd, target_name)
                except (OSError, ValueError) as exc:
                    os.close(parent_fd)
                    raise DraftConflictError(
                        f"Agent YAML target changed since it was first staged: {relative_path}"
                    ) from exc
                if current_fingerprint != draft.base_fingerprints[relative_path]:
                    os.close(parent_fd)
                    raise DraftConflictError(f"Agent YAML target changed since it was first staged: {relative_path}")
                item = _PreparedWrite(
                    relative_path=relative_path,
                    parent_fd=parent_fd,
                    target_name=target_name,
                    temporary_name=temporary_name,
                    backup_name=backup_name,
                    target_path=target,
                    backup_path=target.with_name(backup_name),
                    existed=current_fingerprint is not None,
                    original_fingerprint=current_fingerprint,
                )
                # Register cleanup paths before either preparation write.  A
                # short write or failed backup copy may still leave a file.
                prepared.append(item)
                item.written_fingerprint = _write_exclusive(
                    parent_fd,
                    temporary_name,
                    content,
                    mode=current_fingerprint.mode if current_fingerprint is not None else None,
                )
                if item.existed:
                    backup_fingerprint = _backup_file(parent_fd, target_name, backup_name)
                    if backup_fingerprint != item.original_fingerprint:
                        raise DraftConflictError(
                            f"Agent YAML target changed since it was first staged: {relative_path}"
                        )

            # Validate every original before the first commit so a conflict
            # cannot leave an otherwise untouched multi-file draft half-applied.
            for item in prepared:
                _assert_prepared_parent_unchanged(project_fd, item)
                _assert_prepared_write_unchanged(item)

            attempted: list[_PreparedWrite] = []
            try:
                for item in prepared:
                    _assert_prepared_parent_unchanged(project_fd, item)
                    _assert_prepared_write_unchanged(item)
                    if item.existed:
                        os.replace(
                            item.temporary_name,
                            item.target_name,
                            src_dir_fd=item.parent_fd,
                            dst_dir_fd=item.parent_fd,
                        )
                        attempted.append(item)
                    else:
                        try:
                            os.link(
                                item.temporary_name,
                                item.target_name,
                                src_dir_fd=item.parent_fd,
                                dst_dir_fd=item.parent_fd,
                                follow_symlinks=False,
                            )
                        except FileExistsError as exc:
                            raise DraftConflictError(
                                f"Agent YAML target was created before commit: {item.relative_path}"
                            ) from exc
                        attempted.append(item)
                        _unlink_entry(item.parent_fd, item.temporary_name)
                    applied.append(item.relative_path)
            except Exception as apply_error:
                recovery_details: list[str] = []
                for item in reversed(attempted):
                    try:
                        current_fingerprint = _fingerprint_entry(item.parent_fd, item.target_name)
                    except (OSError, ValueError):
                        current_fingerprint = None
                    if current_fingerprint != item.written_fingerprint:
                        if item.existed and _entry_exists(item.parent_fd, item.backup_name):
                            item.preserve_backup = True
                            recovery_details.append(
                                f"{item.target_path}: changed after commit; rollback skipped; "
                                f"recovery backup preserved at {item.backup_path}"
                            )
                        else:
                            recovery_details.append(
                                f"{item.target_path}: changed after commit; rollback skipped to preserve concurrent content"
                            )
                        continue
                    try:
                        if item.existed and _entry_exists(item.parent_fd, item.backup_name):
                            os.replace(
                                item.backup_name,
                                item.target_name,
                                src_dir_fd=item.parent_fd,
                                dst_dir_fd=item.parent_fd,
                            )
                        else:
                            _unlink_entry(item.parent_fd, item.target_name)
                    except Exception as rollback_error:
                        if item.existed and _entry_exists(item.parent_fd, item.backup_name):
                            item.preserve_backup = True
                            recovery_details.append(
                                f"{item.target_path}: {rollback_error}; recovery backup preserved at {item.backup_path}"
                            )
                        else:
                            recovery_details.append(
                                f"{item.target_path}: {rollback_error}; inspect the target before retrying"
                            )
                if recovery_details:
                    raise RuntimeError(
                        "Agent draft apply failed and rollback was incomplete: " + "; ".join(recovery_details)
                    ) from apply_error
                raise
        finally:
            os.close(project_fd)
            for item in prepared:
                try:
                    _unlink_entry(item.parent_fd, item.temporary_name)
                    if not item.preserve_backup:
                        _unlink_entry(item.parent_fd, item.backup_name)
                finally:
                    os.close(item.parent_fd)

        draft.files.clear()
        draft.base_fingerprints.clear()
        return {"applied": True, "revision": draft.revision, "files": applied}
