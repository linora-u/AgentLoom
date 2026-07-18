"""Bounded conversational assistant used by the AgentLoom TUI.

The configured model answers ordinary questions and can optionally inspect
Agent definitions or edit an in-memory YAML proposal. It cannot run commands,
execute an Agent, or write files. Applying a validated proposal is always an
explicit user operation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from src.tui_bridge.definition import validate_agent_definition

_MAX_TRANSCRIPT_MESSAGES = 16
_MAX_BUILDER_MESSAGE_CHARS = 32_000
_MAX_TRANSCRIPT_CHARS = 64_000
_MAX_ASSISTANT_MESSAGE_CHARS = 32_000
_MAX_INSPECTION_BYTES = 80_000
_TRUNCATION_MARKER = "… [truncated]"


class DraftConflictError(ValueError):
    """Raised when the UI tries to apply a stale draft revision."""


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


def _fsync_directory(directory_fd: int) -> None:
    """Make the preceding directory-entry mutation durable before continuing."""

    os.fsync(directory_fd)


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
                created = False
                try:
                    os.mkdir(part, dir_fd=directory_fd)
                    created = True
                except FileExistsError:
                    pass
                if created:
                    _fsync_directory(directory_fd)
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
        fingerprint = _fingerprint_descriptor(descriptor, name)
        _fsync_directory(directory_fd)
        return fingerprint
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
        _fsync_directory(directory_fd)
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
        return
    _fsync_directory(directory_fd)


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


class _InspectAgentSystemTool:
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


class _StageAgentYamlTool:
    name = "stage_agent_yaml"
    description = (
        "Create or replace one Agent YAML file in the in-memory draft. "
        "The complete YAML must contain non-empty top-level name, description, workflow, "
        "model_type, and tool_call_type fields. Behavioral instructions belong in workflow. "
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


class _ValidateAgentDraftTool:
    name = "validate_agent_draft"
    description = "Validate all staged Agent YAML files and return concrete errors."
    inputs = {}
    output_type = "string"

    def __init__(self, project_root: Path, draft: _Draft):
        self._project_root = project_root
        self._draft = draft

    def forward(self) -> str:
        return json.dumps(_draft_summary(self._project_root, self._draft), ensure_ascii=False)


class BuilderService:
    """Secure draft state plus the independent short-session TUI agent."""

    def __init__(
        self,
        project_root: Path | str,
        *,
        chat_client_factory: Any | None = None,
        retry_sleep: Callable[[float], None] | None = None,
    ):
        self._project_root = Path(project_root).expanduser().resolve()
        self._chat_client_factory = chat_client_factory
        self._retry_sleep = retry_sleep
        self._chat_agent: Any | None = None
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
        on_event: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        session_id = session_id.strip()
        message = message.strip()
        if not session_id:
            raise ValueError("session_id is required")
        if not message:
            raise ValueError("message is required")
        if len(message) > _MAX_BUILDER_MESSAGE_CHARS:
            raise ValueError(f"message must not exceed {_MAX_BUILDER_MESSAGE_CHARS:,} characters")

        return self._send_with_tui_chat_agent(
            session_id=session_id,
            message=message,
            model_type=model_type,
            on_event=on_event,
        )

    def _send_with_tui_chat_agent(
        self,
        *,
        session_id: str,
        message: str,
        model_type: str | None,
        on_event: Callable[[dict[str, object]], None] | None,
    ) -> dict[str, object]:
        # A provider or protocol failure must not poison either the transcript
        # or the staged proposal. Commit the complete turn atomically.
        working_draft = copy.deepcopy(self._draft(session_id))
        candidate_history = [dict(item) for item in self._histories.get(session_id, [])]
        candidate_history.append({"role": "user", "content": message})
        _trim_history(candidate_history)
        tools = self._tools(working_draft)

        if self._chat_agent is None:
            from src.tui_bridge.chat_agent import TuiChatAgent

            kwargs: dict[str, object] = {}
            if self._chat_client_factory is not None:
                kwargs["client_factory"] = self._chat_client_factory
            if self._retry_sleep is not None:
                kwargs["retry_sleep"] = self._retry_sleep
            self._chat_agent = TuiChatAgent(self._project_root, **kwargs)

        result = self._chat_agent.run(
            history=candidate_history,
            model_type=model_type,
            tools=tools,
            on_event=on_event,
        )
        assistant = _bounded_assistant_message(result.assistant)
        candidate_history.append({"role": "assistant", "content": assistant})
        _trim_history(candidate_history)
        self._drafts[session_id] = working_draft
        self._histories[session_id] = candidate_history
        return {
            "session_id": session_id,
            "assistant": assistant,
            "model_type": result.model_type,
            "draft": _draft_summary(self._project_root, working_draft),
        }

    def _tools(self, draft: _Draft) -> list[Any]:
        return [
            _InspectAgentSystemTool(self._project_root),
            _StageAgentYamlTool(self._project_root, draft),
            _ValidateAgentDraftTool(self._project_root, draft),
        ]

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
                        _fsync_directory(item.parent_fd)
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
                        _fsync_directory(item.parent_fd)
                        _unlink_entry(item.parent_fd, item.temporary_name)
                    applied.append(item.relative_path)

                # A rename can detach the final held parent immediately after
                # the last target mutation.  Revalidate every canonical parent
                # before reporting success so a single-file apply cannot claim
                # a write that only exists in a displaced directory.
                for item in prepared:
                    _assert_prepared_parent_unchanged(project_fd, item)
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
                            _fsync_directory(item.parent_fd)
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
