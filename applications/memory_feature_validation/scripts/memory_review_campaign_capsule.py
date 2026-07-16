"""Create and attest an isolated runtime for the real memory campaign.

The release campaign must execute committed source, not the mutable checkout or
its editable virtual environment.  A capsule is a detached worktree with its
own ``uv --locked`` environment and an in-memory ignored model configuration.
Only path-free hashes and boolean origin checks are written to campaign
artifacts; the temporary path and model credentials are never persisted.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CAPSULE_ACTIVE_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_ACTIVE"
CAPSULE_ROOT_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_ROOT"
CAPSULE_TOKEN_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_CAPSULE_TOKEN"
CAPSULE_UV_BINARY_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_UV_BINARY"
CAMPAIGN_LLM_CONFIG_FD_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_FD"
_LEGACY_CAMPAIGN_LLM_CONFIG_SECRET_ENV = (
    "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_SECRET"
)
_TOKEN_FILE = ".agentloom-memory-capsule-token"
_PYCACHE_PREFIX_RELATIVE = Path(".venv") / ".agentloom-pycache"
_MDNS_RESPONDER_SOCKET = "/private/var/run/mDNSResponder"
_TRUSTED_EXECUTION_PREFIXES = (
    "applications/memory_feature_validation",
    "src",
    "config/system.yaml",
    "pyproject.toml",
    "uv.lock",
)
_TRUSTED_IMPORT_SHADOW_PATHS = (
    "applications.py",
    "applications/__init__.py",
    "applications/memory_feature_validation.py",
    "src.py",
)
_ALLOWED_PARENT_ENV = {
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "TZ",
    "USER",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}


def canonical_json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_segments_identity(
    segments: list[bytes],
) -> tuple[str, int]:
    """Hash literal segments and root-boundaries without sentinel collisions."""
    digest = hashlib.sha256()
    digest.update(b"agentloom-capsule-segments-v1\0")
    digest.update(len(segments).to_bytes(8, "big"))
    for segment in segments:
        digest.update(len(segment).to_bytes(8, "big"))
        digest.update(hashlib.sha256(segment).digest())
        digest.update(segment)
    # One abstract byte represents each root occurrence.  Unlike a literal
    # sentinel, the boundary is encoded separately and cannot collide with
    # user-controlled file content.
    canonical_size = sum(map(len, segments)) + max(0, len(segments) - 1)
    return digest.hexdigest(), canonical_size


def _canonical_bytes_identity(data: bytes, root: Path) -> tuple[str, int]:
    raw_root = str(root.resolve()).encode("utf-8")
    return _canonical_segments_identity(data.split(raw_root))


def _canonical_file_identity(path: Path, root: Path) -> tuple[str, int]:
    """Hash bytes with ephemeral capsule-root occurrences as typed boundaries."""
    raw_root = str(root.resolve()).encode("utf-8")
    overlap = b""
    contains_root = False
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            candidate = overlap + chunk
            if raw_root in candidate:
                contains_root = True
                break
            overlap = candidate[-max(0, len(raw_root) - 1) :]
    if not contains_root:
        digest = hashlib.sha256()
        digest.update(b"agentloom-capsule-segments-v1\0")
        digest.update((1).to_bytes(8, "big"))
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(_sha256_file(path)))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), size
    return _canonical_bytes_identity(path.read_bytes(), root)


def _trusted_git() -> Path:
    candidate = shutil.which("git", path=os.defpath)
    if not candidate:
        raise RuntimeError("system git executable is required")
    return Path(candidate).resolve()


def _trusted_uv() -> Path:
    search_path = os.pathsep.join(
        ("/opt/homebrew/bin", "/usr/local/bin", os.defpath)
    )
    candidate = shutil.which("uv", path=search_path)
    if not candidate:
        raise RuntimeError("trusted uv executable is required")
    return Path(candidate).absolute()


def _git_env() -> dict[str, str]:
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"}
    }
    inherited.update(
        {
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return inherited


def _git_argv(*args: str) -> list[str]:
    return [
        str(_trusted_git()),
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "diff.external=",
        *args,
    ]


def _lexically_inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return False
    return True


def _resolved_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _relative_origin(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _private_pycache_prefix(root: Path) -> Path:
    return Path(os.path.abspath(root)) / _PYCACHE_PREFIX_RELATIVE


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        # Command output can contain provider configuration.  Keep the failure
        # typed and deliberately do not echo stdout/stderr into artifacts.
        raise RuntimeError(f"capsule command failed: {Path(command[0]).name}")
    return completed


def _clean_python_env(root: Path, *, token: str, uv: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_PARENT_ENV
    }
    env.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(_private_pycache_prefix(root)),
            "VIRTUAL_ENV": str(root / ".venv"),
            "PATH": os.pathsep.join(
                (str(root / ".venv" / "bin"), str(uv.parent), os.defpath)
            ),
            CAPSULE_ACTIVE_ENV: "1",
            CAPSULE_ROOT_ENV: str(root),
            CAPSULE_TOKEN_ENV: token,
            CAPSULE_UV_BINARY_ENV: str(uv),
        }
    )
    return env


def _write_trusted_loom_launcher(root: Path, python: Path) -> Path:
    """Create the capsule CLI without installing mutable project metadata."""
    root = root.resolve()
    python = Path(os.path.abspath(python))
    private_venv = root / ".venv"
    if not python.is_absolute() or not python.is_file() or not _lexically_inside(
        python, private_venv
    ):
        raise RuntimeError("capsule launcher Python was outside its private venv")
    launcher = private_venv / "bin" / "loom"
    expected_pycache_prefix = str(_private_pycache_prefix(root))
    payload = (
        f"#!{python}\n"
        "import sys\n\n"
        f"_EXPECTED_PYCACHE_PREFIX = {expected_pycache_prefix!r}\n"
        "if (\n"
        "    sys.pycache_prefix != _EXPECTED_PYCACHE_PREFIX\n"
        "    or sys.dont_write_bytecode is not True\n"
        "):\n"
        "    raise RuntimeError(\"capsule bytecode isolation was invalid\")\n"
        "from importlib.util import find_spec\n"
        "from pathlib import Path\n"
        "\n"
        "_CAPSULE_ROOT = Path(__file__).resolve().parents[2]\n"
        "sys.path.append(str(_CAPSULE_ROOT))\n"
        "_SRC_SPEC = find_spec(\"src\")\n"
        "if (\n"
        "    _SRC_SPEC is None\n"
        "    or _SRC_SPEC.origin is None\n"
        "    or Path(_SRC_SPEC.origin).resolve()\n"
        "    != (_CAPSULE_ROOT / \"src\" / \"__init__.py\").resolve()\n"
        "):\n"
        "    raise RuntimeError(\"capsule src import origin was invalid\")\n"
        "from src.__main__ import main\n\n"
        "if __name__ == \"__main__\":\n"
        "    sys.exit(main())\n"
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(launcher, flags, 0o755)
    except FileExistsError as exc:
        raise RuntimeError("capsule loom launcher already existed") from exc
    created_identity: tuple[int, int] | None = None
    try:
        initial = os.fstat(descriptor)
        created_identity = (initial.st_dev, initial.st_ino)
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise RuntimeError("capsule loom launcher was not a new regular file")
        try:
            with (root / "pyproject.toml").open("rb") as handle:
                project_config = tomllib.load(handle)
            configured_entrypoint = project_config["project"]["scripts"]["loom"]
        except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(
                "capsule loom entrypoint contract was invalid"
            ) from exc
        if configured_entrypoint != "src.__main__:main":
            raise RuntimeError("capsule loom entrypoint contract was invalid")
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o755)
        final = launcher.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or final.st_mode & 0o111 != 0o111
            or (final.st_dev, final.st_ino) != created_identity
        ):
            raise RuntimeError("capsule loom launcher identity was invalid")
        return launcher
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current = launcher.lstat()
            if created_identity == (current.st_dev, current.st_ino):
                launcher.unlink()
        except OSError:
            pass
        raise


def _create_private_pycache_prefix(root: Path) -> Path:
    """Create one empty private cache root without accepting existing objects."""
    prefix = _private_pycache_prefix(root)
    try:
        prefix.mkdir(mode=0o700, parents=False, exist_ok=False)
        prefix.chmod(0o700)
        metadata = prefix.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or prefix.is_symlink()
            or any(prefix.iterdir())
        ):
            raise RuntimeError("capsule pycache prefix was invalid")
    except OSError as exc:
        raise RuntimeError("capsule pycache prefix was invalid") from exc
    return prefix


def _pycache_prefix_contract(root: Path) -> dict[str, bool]:
    """Return path-free evidence for the interpreter's private cache root."""
    root = Path(os.path.abspath(root))
    expected = _private_pycache_prefix(root)
    configured_value = sys.pycache_prefix
    configured = (
        Path(configured_value)
        if isinstance(configured_value, str) and configured_value
        else None
    )
    expected_text = str(expected)
    exact = bool(
        configured_value == expected_text
        and sys._xoptions.get("pycache_prefix") == expected_text
        and os.environ.get("PYTHONPYCACHEPREFIX") == expected_text
    )
    inside = bool(configured is not None and _resolved_inside(configured, root))
    empty = False
    read_only = False
    not_symlink = False
    if configured is not None:
        try:
            metadata = configured.lstat()
            not_symlink = not stat.S_ISLNK(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode) and not_symlink:
                empty = not any(configured.iterdir())
                read_only = metadata.st_mode & 0o222 == 0
        except OSError:
            pass
    return {
        "pycache_prefix_exact": exact,
        "pycache_prefix_inside_capsule": inside,
        "pycache_prefix_empty": empty,
        "pycache_prefix_read_only": read_only,
        "pycache_prefix_not_symlink": not_symlink,
    }


@dataclass(frozen=True)
class CampaignCapsule:
    root: Path
    python: Path
    runner: Path
    env: dict[str, str]
    model_config_bytes: bytes


def capsule_is_active() -> bool:
    return os.environ.get(CAPSULE_ACTIVE_ENV) == "1"


def active_capsule_root() -> Path | None:
    if not capsule_is_active():
        return None
    value = str(os.environ.get(CAPSULE_ROOT_ENV) or "").strip()
    if not value:
        return None
    return Path(value).resolve()


def active_capsule_bootstrap_issues(repo_root: Path) -> list[str]:
    """Reject a caller-forged active flag before any campaign work starts."""
    repo_root = repo_root.resolve()
    issues: list[str] = []
    if not capsule_is_active() or active_capsule_root() != repo_root:
        issues.append("capsule active root did not match the executing repository")
    token = str(os.environ.get(CAPSULE_TOKEN_ENV) or "")
    token_file = repo_root / _TOKEN_FILE
    try:
        token_valid = (
            len(token) == 64
            and all(character in "0123456789abcdef" for character in token)
            and token_file.is_file()
            and token_file.read_text(encoding="utf-8") == token
            and token_file.stat().st_mode & 0o077 == 0
        )
    except OSError:
        token_valid = False
    if not token_valid:
        issues.append("capsule private bootstrap token was invalid")
    if not (repo_root / ".git").is_file():
        issues.append("capsule repository was not a linked worktree")
    detached = subprocess.run(
        _git_argv("symbolic-ref", "-q", "HEAD"),
        cwd=repo_root,
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    ).returncode != 0
    if not detached:
        issues.append("capsule worktree was not detached")
    if not _lexically_inside(Path(sys.executable), repo_root / ".venv"):
        issues.append("capsule Python entrypoint was outside its private venv")
    return issues


def _tree_manifest_hash(
    root: Path,
    *,
    excluded_parts: frozenset[str] = frozenset(),
    canonical_root: Path | None = None,
) -> str:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative_path = path.relative_to(root)
        # __pycache__ is derived mutable state, not locked dependency evidence.
        # Skip it before any stat/read so concurrent cache cleanup cannot race us.
        # Loose bytecode stays covered because Python can execute sourceless .pyc.
        if (
            "__pycache__" in relative_path.parts
            or excluded_parts.intersection(relative_path.parts)
        ):
            continue
        relative = relative_path.as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            row: dict[str, Any] = {"path": relative, "kind": "symlink"}
            if canonical_root is None:
                row["target"] = target
            else:
                target_hash, target_bytes = _canonical_bytes_identity(
                    os.fsencode(target),
                    canonical_root,
                )
                row["target_canonical_hash"] = target_hash
                row["target_canonical_bytes"] = target_bytes
            try:
                resolved = path.resolve(strict=True)
                if resolved.is_file():
                    if canonical_root is None:
                        target_hash = _sha256_file(resolved)
                        target_bytes = resolved.stat().st_size
                    else:
                        target_hash, target_bytes = _canonical_file_identity(
                            resolved,
                            canonical_root,
                        )
                    row["target_sha256"] = target_hash
                    row["target_bytes"] = target_bytes
            except OSError:
                row["target_missing"] = True
            rows.append(row)
        elif path.is_file():
            if canonical_root is None:
                file_hash = _sha256_file(path)
                file_bytes = path.stat().st_size
            else:
                file_hash, file_bytes = _canonical_file_identity(
                    path,
                    canonical_root,
                )
            rows.append(
                {
                    "path": relative,
                    "kind": "file",
                    "sha256": file_hash,
                    "bytes": file_bytes,
                    "executable": bool(path.stat().st_mode & 0o111),
                }
            )
        elif path.is_dir():
            rows.append({"path": relative, "kind": "directory"})
    return canonical_json_hash(rows)


def _run_git_bytes(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> bytes:
    completed = subprocess.run(
        _git_argv(*args),
        cwd=cwd,
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError("trusted Git object query failed")
    return bytes(completed.stdout)


def _git_object_format(root: Path) -> str:
    value = _run_git_bytes(
        ["rev-parse", "--show-object-format"],
        cwd=root,
        timeout=30,
    ).decode("ascii", errors="strict").strip()
    if value not in {"sha1", "sha256"}:
        raise RuntimeError("unsupported Git object format")
    return value


def _unsafe_git_customization_issues(root: Path) -> list[str]:
    issues: list[str] = []
    replace_refs = _run_git_bytes(
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
        cwd=root,
        timeout=30,
    )
    if replace_refs.strip():
        issues.append("Git replace refs are not allowed")
    scopes = ["--local"]
    worktree_config = subprocess.run(
        _git_argv("config", "--local", "--bool", "extensions.worktreeConfig"),
        cwd=root,
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if worktree_config.returncode == 0 and (
        worktree_config.stdout.strip().casefold() == b"true"
    ):
        scopes.append("--worktree")
    elif worktree_config.returncode not in {0, 1}:
        raise RuntimeError("trusted Git worktree-config inspection failed")
    for scope in scopes:
        completed = subprocess.run(
            _git_argv(
                "config",
                scope,
                "--name-only",
                "--get-regexp",
                r"^filter\.",
            ),
            cwd=root,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            issues.append("custom Git content filters are not allowed")
        elif completed.returncode not in {0, 1}:
            raise RuntimeError("trusted Git filter inspection failed")
    return list(dict.fromkeys(issues))


def _raw_tree_entries(root: Path, commit: str) -> list[dict[str, str]]:
    output = _run_git_bytes(
        ["ls-tree", "-r", "-z", "--full-tree", commit],
        cwd=root,
        timeout=120,
    )
    rows: list[dict[str, str]] = []
    for raw_row in output.split(b"\0"):
        if not raw_row:
            continue
        metadata, separator, raw_path = raw_row.partition(b"\t")
        if not separator:
            raise RuntimeError("invalid raw Git tree row")
        fields = metadata.decode("ascii", errors="strict").split()
        if len(fields) != 3:
            raise RuntimeError("invalid raw Git tree metadata")
        mode, object_type, oid = fields
        if object_type != "blob" or mode not in {"100644", "100755", "120000"}:
            raise RuntimeError("capsule commit contains an unsupported tree entry")
        try:
            relative = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeError("capsule commit contains a non-UTF-8 path") from exc
        parts = Path(relative).parts
        if (
            not relative
            or Path(relative).is_absolute()
            or any(part in {"", ".", "..", ".git"} for part in parts)
        ):
            raise RuntimeError("capsule commit contains an unsafe path")
        rows.append(
            {
                "path": relative,
                "kind": "symlink" if mode == "120000" else "file",
                "mode": mode,
                "oid": oid,
            }
        )
    if not rows:
        raise RuntimeError("capsule commit tree was empty")
    return sorted(rows, key=lambda row: row["path"])


def trusted_control_plane_matches(root: Path, commit: str) -> bool:
    """Require the historical execution surface to equal the trusted checkout."""
    try:
        expected = []
        for row in _raw_tree_entries(root, commit):
            relative = row["path"]
            parts = Path(relative).parts
            if "__pycache__" in parts or relative.endswith(".pyc"):
                continue
            if any(
                relative == prefix or relative.startswith(f"{prefix}/")
                for prefix in _TRUSTED_EXECUTION_PREFIXES
            ) or relative in _TRUSTED_IMPORT_SHADOW_PATHS:
                expected.append(row)
        object_format = _git_object_format(root)
        candidates: set[Path] = set()
        for relative in (*_TRUSTED_EXECUTION_PREFIXES, *_TRUSTED_IMPORT_SHADOW_PATHS):
            path = root / relative
            if path.is_dir() and not path.is_symlink():
                candidates.update(path.rglob("*"))
            elif path.exists() or path.is_symlink():
                candidates.add(path)
        actual: list[dict[str, str]] = []
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            parts = Path(relative).parts
            if "__pycache__" in parts or relative.endswith(".pyc"):
                continue
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if stat.S_ISLNK(metadata.st_mode):
                data = os.fsencode(os.readlink(path))
                mode = "120000"
                kind = "symlink"
            elif stat.S_ISREG(metadata.st_mode):
                data = path.read_bytes()
                mode = "100755" if metadata.st_mode & 0o111 else "100644"
                kind = "file"
            else:
                return False
            actual.append(
                {
                    "path": relative,
                    "kind": kind,
                    "mode": mode,
                    "oid": _blob_oid(data, object_format),
                }
            )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    return sorted(actual, key=lambda row: row["path"]) == sorted(
        expected,
        key=lambda row: row["path"],
    )


def _blob_oid(data: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _safe_symlink_target(root: Path, destination: Path, data: bytes) -> str:
    try:
        target = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("capsule commit contains a non-UTF-8 symlink") from exc
    if not target or Path(target).is_absolute():
        raise RuntimeError("capsule commit contains an escaping symlink")
    resolved_lexically = Path(os.path.abspath(destination.parent / target))
    if not _lexically_inside(resolved_lexically, root):
        raise RuntimeError("capsule commit contains an escaping symlink")
    return target


def _materialize_commit_tree(root: Path, commit: str) -> list[dict[str, str]]:
    """Materialize raw blobs without checkout, attributes, filters, or hooks."""
    rows = _raw_tree_entries(root, commit)
    object_format = _git_object_format(root)
    process = subprocess.Popen(
        _git_argv("cat-file", "--batch"),
        cwd=root,
        env=_git_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("trusted Git object reader did not start")
    try:
        for row in rows:
            process.stdin.write(row["oid"].encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b"\n")
            fields = header.split()
            if (
                len(fields) != 3
                or fields[0].decode("ascii", errors="strict") != row["oid"]
                or fields[1] != b"blob"
            ):
                raise RuntimeError("trusted Git returned an invalid blob")
            size = int(fields[2])
            data = process.stdout.read(size)
            trailer = process.stdout.read(1)
            if len(data) != size or trailer != b"\n":
                raise RuntimeError("trusted Git returned a truncated blob")
            if _blob_oid(data, object_format) != row["oid"]:
                raise RuntimeError("trusted Git returned a blob with the wrong object id")
            destination = root / row["path"]
            if not _lexically_inside(destination, root):
                raise RuntimeError("capsule materialization escaped its root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                destination.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("capsule tree contains a path collision")
            if row["kind"] == "symlink":
                os.symlink(_safe_symlink_target(root, destination, data), destination)
            else:
                with destination.open("xb") as handle:
                    handle.write(data)
                destination.chmod(0o755 if row["mode"] == "100755" else 0o644)
        process.stdin.close()
        if process.wait(timeout=120) != 0:
            raise RuntimeError("trusted Git object reader failed")
    except Exception:
        process.kill()
        process.wait()
        raise
    return rows


def _checkout_state(root: Path, expected_commit: str) -> tuple[bool, str]:
    expected_rows = _raw_tree_entries(root, expected_commit)
    object_format = _git_object_format(root)
    actual_rows: list[dict[str, str]] = []
    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if not relative_path.parts or relative_path.parts[0] == ".venv":
            continue
        relative = relative_path.as_posix()
        if relative in {".git", _TOKEN_FILE, "config/llm.yaml"}:
            continue
        try:
            metadata = path.lstat()
        except OSError:
            return False, canonical_json_hash(expected_rows)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if stat.S_ISLNK(metadata.st_mode):
            data = os.fsencode(os.readlink(path))
            mode = "120000"
            kind = "symlink"
        elif stat.S_ISREG(metadata.st_mode):
            data = path.read_bytes()
            mode = "100755" if metadata.st_mode & 0o111 else "100644"
            kind = "file"
        else:
            return False, canonical_json_hash(expected_rows)
        actual_rows.append(
            {
                "path": relative,
                "kind": kind,
                "mode": mode,
                "oid": _blob_oid(data, object_format),
            }
        )
    actual_rows.sort(key=lambda row: row["path"])
    return actual_rows == expected_rows, canonical_json_hash(expected_rows)


def _sandbox_binary() -> Path | None:
    if sys.platform != "darwin":
        return None
    candidate = Path("/usr/bin/sandbox-exec")
    return candidate if candidate.is_file() else None


def _git_metadata_paths(repo_root: Path) -> list[Path]:
    """Resolve git-dir/common-dir without executing Git or another binary."""

    def read_pointer(path: Path, *, gitdir: bool) -> str:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("Git metadata pointer was externally aliased")
        raw = path.read_bytes()
        if not raw or len(raw) > 64 * 1024 or b"\0" in raw:
            raise RuntimeError("Git metadata pointer was invalid")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Git metadata pointer was invalid") from exc
        if text.endswith("\n"):
            text = text[:-1]
        if "\n" in text or "\r" in text:
            raise RuntimeError("Git metadata pointer was invalid")
        if gitdir:
            prefix = "gitdir: "
            if not text.startswith(prefix):
                raise RuntimeError("Git metadata pointer was invalid")
            value = text[len(prefix) :]
        else:
            value = text
        if not value or value != value.strip():
            raise RuntimeError("Git metadata pointer was invalid")
        return value

    repo_root = repo_root.resolve()
    dot_git = repo_root / ".git"
    metadata = dot_git.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        git_dir = dot_git.resolve(strict=True)
    elif stat.S_ISREG(metadata.st_mode):
        raw_git_dir = Path(read_pointer(dot_git, gitdir=True))
        git_dir = (
            raw_git_dir if raw_git_dir.is_absolute() else dot_git.parent / raw_git_dir
        ).resolve(strict=True)
    else:
        raise RuntimeError("Git metadata path was externally aliased")
    if not git_dir.is_dir():
        raise RuntimeError("Git metadata path was invalid")

    commondir_pointer = git_dir / "commondir"
    if commondir_pointer.exists() or commondir_pointer.is_symlink():
        raw_common_dir = Path(read_pointer(commondir_pointer, gitdir=False))
        common_dir = (
            raw_common_dir
            if raw_common_dir.is_absolute()
            else git_dir / raw_common_dir
        ).resolve(strict=True)
    else:
        common_dir = git_dir
    if not common_dir.is_dir():
        raise RuntimeError("Git common metadata path was invalid")
    return list(dict.fromkeys((git_dir, common_dir)))


def _git_metadata_is_isolated(paths: list[Path]) -> bool:
    roots = [path.resolve() for path in paths]
    try:
        for root in roots:
            if not root.is_dir():
                return False
            for path in root.rglob("*"):
                metadata = path.lstat()
                is_alternates = (
                    path.name in {"alternates", "http-alternates"}
                    and path.parent.name == "info"
                )
                if stat.S_ISDIR(metadata.st_mode):
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    if metadata.st_nlink != 1:
                        return False
                    if (
                        is_alternates
                        and path.read_text(encoding="utf-8", errors="replace").strip()
                    ):
                        return False
                    continue
                if stat.S_ISSOCK(metadata.st_mode):
                    if metadata.st_nlink != 1:
                        return False
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    if is_alternates:
                        return False
                    resolved = path.resolve(strict=True)
                    if not any(_resolved_inside(resolved, root_path) for root_path in roots):
                        return False
                    continue
                return False
    except OSError:
        return False
    return True


def _require_isolated_git_metadata(repo_root: Path) -> list[Path]:
    paths = _git_metadata_paths(repo_root)
    if not _git_metadata_is_isolated(paths):
        raise RuntimeError("Git metadata is externally aliased")
    return paths


def _sandbox_profile(protected_paths: list[Path]) -> str:
    rules = [
        "(deny network-outbound (remote unix-socket))",
        (
            "(allow network-outbound "
            f'(remote unix-socket (literal "{_MDNS_RESPONDER_SOCKET}")))'
        ),
        "(deny file-link)",
        "(deny file-write-mode)",
        "(deny file-write-owner)",
        "(deny file-write-flags)",
        "(deny file-write-acl)",
    ]
    for path in protected_paths:
        escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
    return "(version 1)(allow default)" + "".join(rules)


def guarded_runtime_command(command: list[str], *, repo_root: Path) -> list[str]:
    sandbox = _sandbox_binary()
    if sandbox is None:
        raise RuntimeError("a supported read-only runtime guard is required")
    dot_git = repo_root / ".git"
    git_metadata_paths = (
        _require_isolated_git_metadata(repo_root)
        if dot_git.exists() or dot_git.is_symlink()
        else []
    )
    profile = _sandbox_profile(
        [
            repo_root.resolve(),
            Path(sys.base_prefix).resolve(),
            *git_metadata_paths,
        ]
    )
    return [str(sandbox), "-p", profile, *command]


def _verify_write_guard(root: Path) -> None:
    probe = root / ".agentloom-write-guard-probe"
    probe.write_text("unchanged", encoding="utf-8")
    command = guarded_runtime_command(
        ["/bin/sh", "-c", f"printf changed > {shlex_quote(str(probe))}"],
        repo_root=root,
    )
    completed = subprocess.run(
        command,
        cwd=root,
        env={"PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    unchanged = probe.read_text(encoding="utf-8") == "unchanged"
    external = root.parent / "write-guard-external"
    external.mkdir(exist_ok=True)
    external_link = external / "linked-probe"
    link_completed = subprocess.run(
        guarded_runtime_command(
            ["/bin/ln", str(probe), str(external_link)],
            repo_root=root,
        ),
        cwd=root,
        env={"PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    external_link_created = external_link.exists()
    probe.unlink()
    shutil.rmtree(external)
    if (
        completed.returncode == 0
        or not unchanged
        or link_completed.returncode == 0
        or external_link_created
    ):
        raise RuntimeError("capsule write guard did not enforce read-only execution")


def shlex_quote(value: str) -> str:
    """Quote one path without importing a shell-oriented campaign dependency."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _freeze_tree(root: Path) -> None:
    paths = sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in paths:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


def _regular_files_unshared(root: Path) -> bool:
    try:
        return all(
            not stat.S_ISREG(path.lstat().st_mode) or path.lstat().st_nlink == 1
            for path in root.rglob("*")
        )
    except OSError:
        return False


def _detach_hardlinked_files(root: Path) -> None:
    """Give every capsule regular file a private inode before execution."""
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink <= 1:
            continue
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.unshare-",
            dir=path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(path, temporary, follow_symlinks=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if not _regular_files_unshared(root):
        raise RuntimeError("capsule contains externally shared regular files")


def _thaw_tree(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(root.stat().st_mode | 0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            continue


def _freeze_capsule(root: Path) -> None:
    # Freeze both source/config and the resolved dependency environment.  The
    # campaign writes runtime/state/artifacts only under the external output
    # directory supplied by the parent process.
    _freeze_tree(root / ".venv")
    for child in root.iterdir():
        if child.name == ".venv" or child.is_symlink():
            continue
        if child.is_dir():
            _freeze_tree(child)
        else:
            child.chmod(child.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


def _tree_is_read_only(root: Path) -> bool:
    try:
        return all(
            path.is_symlink() or path.stat().st_mode & 0o222 == 0
            for path in (root, *root.rglob("*"))
        )
    except OSError:
        return False


def _secure_cleanup(repo_root: Path, temporary_root: Path, capsule_root: Path) -> None:
    # New capsules never write the credential-bearing config. Remove a file
    # defensively so interrupted runs from an older harness are also cleaned.
    _thaw_tree(capsule_root)
    private_config = capsule_root / "config" / "llm.yaml"
    try:
        if private_config.exists():
            private_config.write_bytes(b"")
            private_config.unlink()
    except OSError:
        pass
    try:
        subprocess.run(
            _git_argv(
                "worktree",
                "remove",
                "--force",
                str(capsule_root),
            ),
            cwd=repo_root,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass
    for _ in range(2):
        if not temporary_root.exists():
            break
        _thaw_tree(temporary_root)
        try:
            shutil.rmtree(temporary_root)
        except OSError:
            continue
    try:
        subprocess.run(
            _git_argv("worktree", "prune"),
            cwd=repo_root,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass
    if temporary_root.exists():
        raise RuntimeError("capsule cleanup did not remove its private temporary root")


@contextmanager
def provision_capsule(
    repo_root: Path,
    *,
    expected_commit: str,
) -> Iterator[CampaignCapsule]:
    """Yield a detached, locked, private runtime and remove it afterwards."""
    repo_root = repo_root.resolve()
    _require_isolated_git_metadata(repo_root)
    uv = _trusted_uv()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise RuntimeError("capsule requires one fixed commit")
    model_config = repo_root / "config" / "llm.yaml"
    if not model_config.is_file():
        raise RuntimeError("summary model configuration is missing")
    model_config_bytes = model_config.read_bytes()
    customization_issues = _unsafe_git_customization_issues(repo_root)
    if customization_issues:
        raise RuntimeError("unsafe Git customization is active")

    temporary_root = Path(
        tempfile.mkdtemp(prefix="agentloom-memory-capsule-")
    ).resolve()
    temporary_root.chmod(0o700)
    capsule_root = temporary_root / "checkout"
    try:
        _run_checked(
            _git_argv(
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                str(capsule_root),
                expected_commit,
            ),
            cwd=repo_root,
            env=_git_env(),
            timeout=120,
        )
        _materialize_commit_tree(capsule_root, expected_commit)
        token = secrets.token_hex(32)
        token_file = capsule_root / _TOKEN_FILE
        token_file.write_text(token, encoding="utf-8")
        token_file.chmod(0o600)
        env = _clean_python_env(capsule_root, token=token, uv=uv)
        _run_checked(
            [
                str(uv),
                "sync",
                "--locked",
                "--all-groups",
                "--quiet",
                "--no-install-project",
                "--python",
                str(Path(sys.executable).resolve()),
            ],
            cwd=capsule_root,
            env=env,
            timeout=1800,
        )
        _run_checked(
            [str(uv), "lock", "--check"],
            cwd=capsule_root,
            env=env,
            timeout=120,
        )
        python = capsule_root / ".venv" / "bin" / "python"
        runner = (
            capsule_root
            / "applications"
            / "memory_feature_validation"
            / "scripts"
            / "run_memory_review_campaign.py"
        )
        if not python.is_file() or not runner.is_file():
            raise RuntimeError("capsule runtime entrypoints are missing")
        _write_trusted_loom_launcher(capsule_root, python)
        _run_checked(
            [
                str(uv),
                "sync",
                "--locked",
                "--all-groups",
                "--no-install-project",
                "--check",
            ],
            cwd=capsule_root,
            env=env,
            timeout=300,
        )
        _detach_hardlinked_files(capsule_root)

        # Verify the exact checkout without importing campaign code; this also
        # keeps provisioning valid while the parent is testing a previous
        # committed revision. The child repeats the full manifest check.
        actual_commit = _run_checked(
            _git_argv("rev-parse", "HEAD"),
            cwd=capsule_root,
            env=_git_env(),
            timeout=30,
        ).stdout.strip()
        checkout_exact, _ = _checkout_state(capsule_root, expected_commit)
        if actual_commit != expected_commit or not checkout_exact:
            raise RuntimeError("capsule source does not match its detached commit")
        _verify_write_guard(capsule_root)
        _create_private_pycache_prefix(capsule_root)
        _freeze_capsule(capsule_root)
        yield CampaignCapsule(
            capsule_root,
            python,
            runner,
            env,
            model_config_bytes,
        )
    finally:
        _secure_cleanup(repo_root, temporary_root, capsule_root)


def build_capsule_descriptor(
    *,
    repo_root: Path,
    runner_file: Path,
    source: dict[str, Any],
    dataset: dict[str, Any],
    model_contract: dict[str, Any],
    model_config_memory_only: bool,
) -> dict[str, Any]:
    """Return path-free evidence for the currently executing capsule."""
    repo_root = repo_root.resolve()
    python = Path(sys.executable)
    loom = repo_root / ".venv" / "bin" / "loom"
    uv_value = str(os.environ.get(CAPSULE_UV_BINARY_ENV) or "")
    uv = Path(uv_value).absolute() if uv_value else None
    git = _trusted_git()
    sandbox = _sandbox_binary()
    try:
        git_metadata_paths = _git_metadata_paths(repo_root)
        git_metadata_write_guarded = bool(
            git_metadata_paths and _git_metadata_is_isolated(git_metadata_paths)
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        git_metadata_write_guarded = False
    uv_version = ""
    lock_sync_ok = False
    if uv and uv.is_file():
        try:
            uv_version = _run_checked(
                [str(uv), "--version"], cwd=repo_root, timeout=30
            ).stdout.strip()
            _run_checked(
                [str(uv), "lock", "--check"], cwd=repo_root, timeout=120
            )
            _run_checked(
                [
                    str(uv),
                    "sync",
                    "--locked",
                    "--all-groups",
                    "--no-install-project",
                    "--check",
                ],
                cwd=repo_root,
                timeout=300,
            )
            lock_sync_ok = True
        except (OSError, RuntimeError, subprocess.SubprocessError):
            lock_sync_ok = False

    src_spec = importlib.util.find_spec("src")
    src_origin = Path(src_spec.origin) if src_spec and src_spec.origin else Path()
    shebang_target = Path()
    if loom.is_file():
        first_line = loom.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if first_line.startswith("#!"):
            shebang_target = Path(first_line[2:].strip().split()[0])

    distributions = sorted(
        {
            (
                str(distribution.metadata.get("Name") or "").casefold(),
                str(distribution.version or ""),
            )
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        }
    )
    try:
        shebang_matches_python = bool(
            shebang_target.is_file()
            and python.is_file()
            and os.path.samefile(shebang_target, python)
        )
    except OSError:
        shebang_matches_python = False
    loom_hash = (
        _canonical_file_identity(loom, repo_root)[0]
        if loom.is_file()
        else ""
    )
    model_config = repo_root / "config" / "llm.yaml"
    expected_commit = str(source.get("commit") or "")
    checkout_exact, checkout_manifest_hash = _checkout_state(
        repo_root,
        expected_commit,
    )
    git_version = _run_checked(
        _git_argv("--version"),
        cwd=repo_root,
        env=_git_env(),
        timeout=30,
    ).stdout.strip()
    runtime_env = {
        key: hashlib.sha256(value.encode()).hexdigest()
        for key, value in sorted(os.environ.items())
        if key
        not in {
            CAPSULE_TOKEN_ENV,
            CAMPAIGN_LLM_CONFIG_FD_ENV,
            _LEGACY_CAMPAIGN_LLM_CONFIG_SECRET_ENV,
            CAPSULE_ROOT_ENV,
            "PATH",
            "PYTHONPYCACHEPREFIX",
            "VIRTUAL_ENV",
        }
    }
    stdlib_root = Path(sysconfig.get_path("stdlib"))
    pycache_contract = _pycache_prefix_contract(repo_root)
    descriptor: dict[str, Any] = {
        "schema_version": 1,
        "git_commit": str(source.get("commit") or ""),
        "source_manifest_hash": canonical_json_hash(source.get("files") or []),
        "dataset_manifest_hash": canonical_json_hash(dataset),
        "model_contract_hash": canonical_json_hash(model_contract),
        "uv_lock_hash": _sha256_file(repo_root / "uv.lock")
        if (repo_root / "uv.lock").is_file()
        else "",
        "uv_version": uv_version,
        "uv_binary_hash": _sha256_file(uv) if uv and uv.is_file() else "",
        "git_version": git_version,
        "git_binary_hash": _sha256_file(git),
        "lock_sync_ok": lock_sync_ok,
        "python_version": sys.version,
        "python_cache_tag": str(sys.implementation.cache_tag or ""),
        "python_binary_hash": _sha256_file(python) if python.is_file() else "",
        "loom_hash": loom_hash,
        "loom_shebang_target_hash": (
            _sha256_file(shebang_target) if shebang_target.is_file() else ""
        ),
        "distribution_set_hash": canonical_json_hash(distributions),
        "venv_manifest_hash": _tree_manifest_hash(
            repo_root / ".venv",
            canonical_root=repo_root,
        )
        if (repo_root / ".venv").is_dir()
        else "",
        "stdlib_manifest_hash": _tree_manifest_hash(
            stdlib_root,
            excluded_parts=frozenset({"site-packages", "dist-packages"}),
        )
        if stdlib_root.is_dir()
        else "",
        "checkout_manifest_hash": checkout_manifest_hash,
        "runtime_env_contract_hash": canonical_json_hash(runtime_env),
        "write_guard_binary_hash": _sha256_file(sandbox)
        if sandbox is not None
        else "",
        "bootstrap_valid": not active_capsule_bootstrap_issues(repo_root),
        "checkout_exact": checkout_exact,
        "write_guard_available": sandbox is not None,
        "python_is_capsule": _lexically_inside(python, repo_root / ".venv"),
        "python_prefix_is_capsule": Path(os.path.abspath(sys.prefix))
        == Path(os.path.abspath(repo_root / ".venv")),
        "loom_is_capsule": _resolved_inside(loom, repo_root / ".venv"),
        "loom_shebang_is_capsule": _lexically_inside(
            shebang_target, repo_root / ".venv"
        ),
        "loom_shebang_matches_python": shebang_matches_python,
        "src_origin_is_capsule": _resolved_inside(src_origin, repo_root),
        "runner_origin_is_capsule": _resolved_inside(runner_file, repo_root),
        "src_origin_relative": _relative_origin(src_origin, repo_root),
        "runner_origin_relative": _relative_origin(runner_file, repo_root),
        "user_site_disabled": os.environ.get("PYTHONNOUSERSITE") == "1",
        "bytecode_writes_disabled": os.environ.get("PYTHONDONTWRITEBYTECODE") == "1",
        "python_dont_write_bytecode": sys.dont_write_bytecode is True,
        **pycache_contract,
        "capsule_tree_read_only": _tree_is_read_only(repo_root),
        "capsule_files_unshared": _regular_files_unshared(repo_root),
        "git_metadata_write_guarded": git_metadata_write_guarded,
        "model_config_memory_only": bool(
            not model_config.exists() and model_config_memory_only
        ),
        "private_parent": bool(repo_root.parent.stat().st_mode & 0o077 == 0),
    }
    descriptor["capsule_id"] = canonical_json_hash(descriptor)
    return descriptor


def capsule_descriptor_issues(descriptor: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    digest_fields = (
        "source_manifest_hash",
        "dataset_manifest_hash",
        "model_contract_hash",
        "uv_lock_hash",
        "uv_binary_hash",
        "git_binary_hash",
        "python_binary_hash",
        "loom_hash",
        "loom_shebang_target_hash",
        "distribution_set_hash",
        "venv_manifest_hash",
        "stdlib_manifest_hash",
        "checkout_manifest_hash",
        "runtime_env_contract_hash",
        "write_guard_binary_hash",
        "capsule_id",
    )
    if descriptor.get("schema_version") != 1:
        issues.append("capsule descriptor schema was invalid")
    if not all(
        isinstance(descriptor.get(field), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(descriptor.get(field))) is not None
        for field in digest_fields
    ):
        issues.append("capsule descriptor hashes were incomplete")
    if not re.fullmatch(r"[0-9a-f]{40}", str(descriptor.get("git_commit") or "")):
        issues.append("capsule commit was invalid")
    if not str(descriptor.get("uv_version") or "").strip():
        issues.append("capsule uv version was missing")
    if not str(descriptor.get("git_version") or "").strip():
        issues.append("capsule git version was missing")
    if not str(descriptor.get("python_version") or "").strip() or not str(
        descriptor.get("python_cache_tag") or ""
    ).strip():
        issues.append("capsule Python identity was missing")
    required_true = (
        "lock_sync_ok",
        "bootstrap_valid",
        "checkout_exact",
        "write_guard_available",
        "python_is_capsule",
        "python_prefix_is_capsule",
        "loom_is_capsule",
        "loom_shebang_is_capsule",
        "loom_shebang_matches_python",
        "src_origin_is_capsule",
        "runner_origin_is_capsule",
        "user_site_disabled",
        "bytecode_writes_disabled",
        "python_dont_write_bytecode",
        "pycache_prefix_exact",
        "pycache_prefix_inside_capsule",
        "pycache_prefix_empty",
        "pycache_prefix_read_only",
        "pycache_prefix_not_symlink",
        "capsule_tree_read_only",
        "capsule_files_unshared",
        "git_metadata_write_guarded",
        "model_config_memory_only",
        "private_parent",
    )
    if not all(descriptor.get(field) is True for field in required_true):
        issues.append("capsule runtime escaped its isolated locked environment")
    expected_runner = (
        "applications/memory_feature_validation/scripts/"
        "run_memory_review_campaign.py"
    )
    if descriptor.get("src_origin_relative") != "src/__init__.py":
        issues.append("capsule src import origin was invalid")
    if descriptor.get("runner_origin_relative") != expected_runner:
        issues.append("capsule runner origin was invalid")
    identity = dict(descriptor)
    recorded_id = identity.pop("capsule_id", "")
    if recorded_id != canonical_json_hash(identity):
        issues.append("capsule id did not match its descriptor")
    return issues
