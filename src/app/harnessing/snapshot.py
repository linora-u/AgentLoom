"""Content-addressed Application resources, independent of execution directories.

Archives contain resource bytes and credential *references*. A caller must bind
private values separately; an archived reference is never a working credential.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

_EXCLUDED = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".agentloom", ".pytest_cache", ".ruff_cache", "data", "runtime", "output", "outputs", "results", "logs", "dist"})
_SECRET_FILES = frozenset({".env", "auth.json", "credentials.json", "id_rsa", "id_ed25519"})
_SECRET_KEYS = frozenset({"api_key", "authorization", "authorizations", "fallback_authorizations", "password", "secret", "secret_key", "private_key", "access_token", "refresh_token", "token", "client_secret", "cookie"})
_CONFIG_EXTENSIONS = frozenset({".yaml", ".yml", ".json"})
_EDITABLE = frozenset({"system_prompt", "skills", "tools", "toolsets", "mcp_servers", "worker_agents"})
_MAX_FILES = 16384
_MAX_BYTES = 256 * 1024 * 1024


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or "\\" in value:
        raise ValueError("Bundle paths must be canonical project-relative paths")
    return path


def _secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(("_api_key", "_password", "_secret", "_token", "_authorization", "_authorizations"))


def _sanitize(value: Any, source: str, refs: list[str], pointer: str = "", credentials: MutableMapping | None = None) -> Any:
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
            private_url = parsed.username is not None or parsed.password is not None or any(_secret_key(key) for key, _ in parse_qsl(parsed.query))
        except ValueError:
            private_url = False
        if private_url:
            ref = source + "#" + pointer
            refs.append(ref)
            if credentials is not None:
                credentials[ref] = value
            return "harness-credential-ref:" + ref
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Snapshot configuration keys must be strings")
            location = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            schema_definition = isinstance(item, dict) and "type" in item and "/properties" in pointer
            if _secret_key(key) and not schema_definition and item not in (None, "", {}):
                ref = source + "#" + location
                refs.append(ref)
                if credentials is not None:
                    credentials[ref] = copy.deepcopy(item)
                result[key] = "harness-credential-ref:" + ref
            else:
                result[key] = _sanitize(item, source, refs, location, credentials)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, source, refs, pointer + "/" + str(index), credentials) for index, item in enumerate(value)]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("Snapshot configuration must be JSON serializable")


def _references(value: Any):
    if not isinstance(value, dict):
        return
    prompt = value.get("system_prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("path"), str):
        yield prompt["path"]
    skills = value.get("skills")
    if isinstance(skills, dict):
        yield from (path for path in skills.get("paths", []) if isinstance(path, str))
    mcp = value.get("mcp_servers")
    if isinstance(mcp, str):
        yield mcp
    elif isinstance(mcp, list):
        yield from (path for path in mcp if isinstance(path, str))
    elif isinstance(mcp, dict):
        if isinstance(mcp.get("path"), str):
            yield mcp["path"]
        yield from (path for path in mcp.get("paths", []) if isinstance(path, str))
    runtime = value.get("runtime_options")
    if isinstance(runtime, dict) and isinstance(runtime.get("prompt_template_path"), str):
        yield runtime["prompt_template_path"]


def _portable_paths(value: Any, owner: Path, project: Path) -> Any:
    value = copy.deepcopy(value)
    if not isinstance(value, dict):
        return value
    def portable(path):
        if isinstance(path, str) and Path(path).is_absolute() and Path(path).is_relative_to(project):
            return os.path.relpath(path, owner)
        return path
    for key in ("system_prompt", "skills", "runtime_options"):
        section = value.get(key)
        if isinstance(section, dict):
            for name in ("path", "prompt_template_path"):
                if name in section:
                    section[name] = portable(section[name])
            if isinstance(section.get("paths"), list):
                section["paths"] = list(map(portable, section["paths"]))
    mcp = value.get("mcp_servers")
    if isinstance(mcp, str):
        value["mcp_servers"] = portable(mcp)
    elif isinstance(mcp, list):
        value["mcp_servers"] = list(map(portable, mcp))
    elif isinstance(mcp, dict):
        if "path" in mcp:
            mcp["path"] = portable(mcp["path"])
        if isinstance(mcp.get("paths"), list):
            mcp["paths"] = list(map(portable, mcp["paths"]))
    return value


@dataclass(frozen=True)
class CandidateBundle:
    candidate_id: str
    root: Path
    application: str
    manifest: Mapping[str, Any]

    @property
    def project_root(self) -> Path:
        return self.root / "project"

    @property
    def application_root(self) -> Path:
        return self.project_root / self.application

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "root": str(self.root), "application": self.application}

    def verify(self) -> None:
        actual = json.loads((self.root / "manifest.json").read_text())
        if actual != self.manifest or _digest(_json(actual)) != self.candidate_id:
            raise ValueError("Candidate manifest identity changed")
        expected = actual["files"]
        actual_files = set()
        for tree in (self.root / "project", self.root / "python"):
            for path in tree.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Candidate contains a symbolic link")
                if path.is_file():
                    logical = path.relative_to(self.root).as_posix()
                    actual_files.add(logical)
                    entry = expected.get(logical)
                    if entry is None or _digest(path.read_bytes()) != entry["hash"]:
                        raise ValueError("Candidate resource changed: " + logical)
                    if bool(path.stat().st_mode & stat.S_IXUSR) != entry["executable"]:
                        raise ValueError("Candidate executable permission changed: " + logical)
        if actual_files != set(expected):
            raise ValueError("Candidate resources are missing")


def load_bundle(path: Path | str) -> CandidateBundle:
    root = Path(path).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Candidate bundle must be a real directory")
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported candidate bundle version")
    application = _relative(manifest["application"]).as_posix()
    bundle = CandidateBundle(_digest(_json(manifest)), root, application, manifest)
    bundle.verify()
    return bundle


def _read_regular(path: Path, boundary: Path) -> bytes:
    if path.is_symlink():
        raise ValueError("Candidate resource symlinks require explicit materialization: " + str(path))
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(boundary):
        raise ValueError("Candidate resource escapes project boundary")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Candidate resource must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(_MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Candidate resource changed while snapshotting")
        if len(payload) > _MAX_BYTES:
            raise ValueError("Candidate resource exceeds snapshot size limit")
        return payload
    finally:
        os.close(descriptor)


def _files(root: Path):
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in _EXCLUDED)
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise ValueError("Candidate resource directory cannot be a symbolic link")
        for name in sorted(names):
            if name in _SECRET_FILES or name.endswith((".pyc", ".sqlite", ".db", ".log")):
                continue
            yield Path(parent) / name


def _commit(staging: Path, destination: Path, application: str, *, credential_refs: Sequence[str], effective_config: Mapping | None, model_config: Mapping | None, environment: Sequence[dict], editable_resources: Mapping | None = None) -> CandidateBundle:
    files = {}
    total = 0
    for root in (staging / "project", staging / "python"):
        root.mkdir(exist_ok=True)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                payload = path.read_bytes()
                total += len(payload)
                if len(files) >= _MAX_FILES or total > _MAX_BYTES:
                    raise ValueError("Candidate dependency closure exceeds bounded snapshot limits")
                files[path.relative_to(staging).as_posix()] = {"hash": _digest(payload), "bytes": len(payload), "executable": bool(path.stat().st_mode & stat.S_IXUSR)}
    # Only file-backed capabilities are restored from source#JSONpointer. The
    # effective catalog has its own trusted in-memory binding and must not
    # manufacture non-file references for a later controller recovery.
    refs = list(credential_refs)
    manifest = {"schema_version": 1, "application": application, "files": files,
                "effective_config": _sanitize(effective_config or {}, "effective_config", []),
                "model_config": _sanitize(model_config or {}, "model_config", []),
                "credential_refs": sorted(set(refs)), "environment": list(environment), "editable_resources": dict(editable_resources or {})}
    identity = _digest(_json(manifest))
    final = destination / identity.split(":", 1)[1]
    destination.mkdir(parents=True, exist_ok=True)
    if final.exists():
        return load_bundle(final)
    (staging / "manifest.json").write_bytes(_json(manifest))
    for path in staging.rglob("*"):
        if path.is_file():
            path.chmod(0o555 if path.stat().st_mode & stat.S_IXUSR else 0o444)
    os.rename(staging, final)
    return CandidateBundle(identity, final, application, manifest)


def snapshot_application(project_root: Path, application_root: Path, destination: Path, *, additional_resources: Sequence[Path] = (), excluded_paths: Sequence[Path] = (), editable_resources: Mapping[str, str] | None = None, credential_sink: MutableMapping[str, Any] | None = None, effective_config: Mapping | None = None, model_config: Mapping | None = None) -> CandidateBundle:
    """Freeze declared resources and fixed code without copying business databases.

    References outside the Application must be under the project and declared
    through project Skills, known configuration or additional_resources. A
    credential sentinel must be resolved by the trusted execution driver.
    """
    project = Path(project_root).resolve(strict=True)
    application_path = Path(application_root).resolve(strict=True)
    application = application_path.relative_to(project).as_posix()
    _relative(application)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".bundle-", dir=destination))
    refs: list[str] = []
    excluded = tuple(Path(path).resolve() for path in excluded_paths)
    mutable = dict(editable_resources or {})
    for name, component in mutable.items():
        if not _relative(name).is_relative_to(Path(application)) or component not in _EDITABLE:
            raise ValueError("Invalid driver-declared editable resource")
    sources: set[Path] = set(_files(application_path))
    for directory in (project / "skills", project / "config"):
        if directory.exists():
            sources.update(_files(directory))
    for name in ("pyproject.toml",):
        if (project / name).is_file():
            sources.add(project / name)
    for resource in additional_resources:
        path = Path(resource)
        path = path if path.is_absolute() else project / path
        if not path.resolve(strict=True).is_relative_to(project):
            raise ValueError("Additional resource must be inside project")
        sources.update(_files(path) if path.is_dir() else (path,))
    try:
        # Discover configured references outside the application (for example a
        # project-wide prompt or Skill directory), retaining original ownership.
        pending = list(sources)
        scanned = set()
        while pending:
            source = pending.pop()
            if source in scanned or source.suffix not in _CONFIG_EXTENSIONS:
                continue
            scanned.add(source)
            if any(source.resolve().is_relative_to(path) for path in excluded):
                continue
            raw = _read_regular(source, project)
            parsed = json.loads(raw) if source.suffix == ".json" else yaml.safe_load(raw)
            owner = application_path if source.is_relative_to(application_path) else project
            for reference in _references(parsed):
                target = Path(reference).expanduser()
                target = target if target.is_absolute() else owner / target
                if not target.resolve(strict=True).is_relative_to(project):
                    raise ValueError("Declared resource escapes project; materialize it explicitly")
                if any(target.resolve().is_relative_to(path) for path in excluded):
                    raise ValueError("Candidate resource references hidden evaluation data")
                discovered = set(_files(target)) if target.is_dir() else {target}
                pending.extend(discovered - sources)
                sources.update(discovered)
        for source in sorted(sources):
            if any(source.resolve().is_relative_to(path) for path in excluded):
                continue
            relative = source.relative_to(project)
            target = staging / "project" / relative
            payload = _read_regular(source, project)
            if source.suffix in _CONFIG_EXTENSIONS:
                parsed = json.loads(payload) if source.suffix == ".json" else yaml.safe_load(payload)
                if isinstance(parsed, (dict, list)):
                    owner = application_path if source.is_relative_to(application_path) else project
                    sanitized = _portable_paths(_sanitize(parsed, relative.as_posix(), refs, credentials=credential_sink), owner, project)
                    payload = _json(sanitized) if source.suffix == ".json" else yaml.safe_dump(sanitized, allow_unicode=True, sort_keys=True).encode()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            target.chmod(0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644)
        # Archive the imported framework itself, not a live editable-install path.
        framework = Path(__file__).resolve().parents[2]
        for source in _files(framework):
            target = staging / "python" / "agentloom" / source.relative_to(framework)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_read_regular(source, framework))
            target.chmod(0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644)
        environment = sorted(({"name": item.metadata.get("Name", ""), "version": item.version} for item in distributions()), key=lambda item: (item["name"], item["version"]))
        return _commit(staging, destination, application, credential_refs=refs, effective_config=effective_config, model_config=model_config, environment=environment, editable_resources=mutable)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _surface(relative: Path, before: bytes | None, after: bytes | None) -> set[str]:
    parts = relative.parts
    if "skills" in parts:
        return {"skills"}
    if relative.suffix in {".yaml", ".yml"}:
        old = yaml.safe_load(before) if before else {}
        new = yaml.safe_load(after) if after else {}
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise ValueError("Agent and system configuration must be mappings")
        changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
        if before is None or after is None:
            if "workflows" not in parts:
                raise ValueError("Only Agent definitions can be added or removed")
            return {"worker_agents"}
        if not changed <= _EDITABLE:
            raise ValueError("Candidate changes fixed configuration fields: " + ", ".join(sorted(changed - _EDITABLE)))
        return changed
    if relative.suffix.lower() in {".md", ".txt", ".j2"} and any(name in parts for name in ("agent", "prompts", "prompt")):
        return {"system_prompt"}
    if relative.suffix == ".json" and relative.name.startswith(".mcp"):
        return {"mcp_servers"}
    raise ValueError("Candidate cannot edit fixed code or undeclared resources: " + relative.as_posix())


def _check_candidate_secrets(relative: Path, old: Any, new: Any, pointer: str = "") -> None:
    prefix = "harness-credential-ref:"
    def contains_reference(value):
        if isinstance(value, str):
            return value.startswith(prefix)
        if isinstance(value, dict):
            return any(contains_reference(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_reference(item) for item in value)
        return False
    if isinstance(new, str) and new.startswith(prefix):
        if new != prefix + relative.as_posix() + "#" + pointer or new != old:
            raise ValueError("Candidate cannot move or introduce a private capability reference")
    if isinstance(new, dict):
        previous = old if isinstance(old, dict) else {}
        if contains_reference(previous) and any(key in previous for key in ("command", "url")):
            for key in ("type", "command", "args", "url", "env", "headers"):
                if previous.get(key) != new.get(key):
                    raise ValueError("Candidate cannot redirect a credential-bearing server capability")
        for key, value in new.items():
            if _secret_key(str(key)) and value not in (None, "", {}):
                if not isinstance(value, str) or not value.startswith(prefix):
                    raise ValueError("Candidate cannot introduce private credential values")
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            _check_candidate_secrets(relative, previous.get(key), value, pointer + "/" + escaped)
    elif isinstance(new, list):
        previous = old if isinstance(old, list) else []
        for index, item in enumerate(new):
            _check_candidate_secrets(relative, previous[index] if index < len(previous) else None, item, pointer + "/" + str(index))


def derive_candidate(base: CandidateBundle, changes: Mapping[str, str | None], destination: Path, *, editable: Sequence[str], validate: Callable[[Path, Path], None] | None = None) -> CandidateBundle:
    base.verify()
    allowed = set(editable)
    if not allowed <= _EDITABLE:
        raise ValueError("Unsupported editable component")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=destination))
    try:
        for name in ("project", "python"):
            shutil.copytree(base.root / name, staging / name, dirs_exist_ok=True)
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644)
        for name, value in changes.items():
            relative = _relative(name)
            if not relative.is_relative_to(Path(base.application)) and not relative.is_relative_to(Path("skills")):
                raise ValueError("Candidate edit must belong to this Application or its Skills")
            if value is not None and not isinstance(value, str):
                raise ValueError("Candidate edits must contain UTF-8 text or null")
            target = staging / "project" / relative
            before = target.read_bytes() if target.is_file() else None
            after = value.encode() if value is not None else None
            registered = base.manifest.get("editable_resources", {}).get(name)
            surfaces = {registered} if registered is not None else _surface(relative, before, after)
            if registered is not None and validate is None:
                raise ValueError("Driver-declared mutable resources require the registered validator")
            if not surfaces <= allowed:
                raise ValueError("Candidate edit exceeds configured editable components")
            if value is None:
                if not target.is_file():
                    raise ValueError("Cannot delete a missing candidate resource")
                target.unlink()
            else:
                if relative.suffix in _CONFIG_EXTENSIONS:
                    parse = json.loads if relative.suffix == ".json" else yaml.safe_load
                    _check_candidate_secrets(relative, parse(before) if before else None, parse(after))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(after)
        if validate is not None:
            validate(staging / "project", staging / "project" / base.application)
        else:
            _validate_definitions(staging / "project", staging / "project" / base.application)
        return _commit(staging, destination, base.application, credential_refs=base.manifest["credential_refs"], effective_config=base.manifest["effective_config"], model_config=base.manifest["model_config"], environment=base.manifest["environment"], editable_resources=base.manifest.get("editable_resources", {}))
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _validate_definitions(project: Path, application: Path) -> None:
    from agentloom.app.definition import inspect_application_definition
    from agentloom.config.config import load_project_config
    config = load_project_config(project)
    for source in sorted((application / "workflows").rglob("*.yaml")):
        parsed = yaml.safe_load(source.read_text())
        if not isinstance(parsed, dict):
            raise ValueError("Agent definition must be a mapping")
        result = inspect_application_definition(project, str(source), parsed, root_is_worker=True, base_config=config)
        if result.errors:
            raise ValueError("Invalid candidate topology: " + "; ".join(result.errors))
