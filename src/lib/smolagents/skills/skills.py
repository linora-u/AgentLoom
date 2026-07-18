"""Skills manager — loads Skill metadata, resources, prompts, and scripts.

Data classes, parsing logic, and script execution live in sibling modules
(``parser`` and ``executors``) to keep this file focused on orchestration.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from smolagents import AgentLogger
from src.lib.logging import get_logger

from .executors import (  # noqa: F401
    OUTPUT_PREVIEW_MAX_BYTES,
    SkillOutputSnapshot,
    SkillSubprocessCapture,
    run_skill_subprocess,
)

# Re-export data classes so existing ``from .skills import ...`` still works.
from .parser import (  # noqa: F401
    SKILLS_PROMPT,
    Skill,
    SkillContent,
    SkillMetadata,
    build_skills_prompt,
    parse_skill_file,
)

SKILL_INLINE_MAX_CHARS = 18000
SKILL_INLINE_PREVIEW_LINES = 180
SKILL_RESOURCE_TEXT_MAX_CHARS = 120000
_UNSET = object()
_RESOURCE_DIR_EXCLUDES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}


class SkillsManager:
    """Manages loading and activation of skills."""

    _instance: SkillsManager | None = None

    def __init__(self, logger: AgentLogger | None = None):
        self.skills: dict[str, Skill] = {}
        self._logger = get_logger(logger, __name__)
        self._tools_mapping: dict[str, dict[str, str]] = {}

    # -- Logger / mapping helpers -------------------------------------------

    def set_logger(self, logger: AgentLogger | None):
        self._logger = get_logger(logger, __name__)

    def set_tools_mapping(self, mapping: dict[str, dict[str, str]]):
        self._tools_mapping = mapping

    def get_tool_mapping_for_skill(self, skill_name: str) -> dict[str, str]:
        skill = self.skills.get(skill_name)
        if not skill:
            return {}
        platform = skill.metadata.platform or "Claude"
        return self._tools_mapping.get(platform, {})

    def get_skill(self, skill_name: str) -> Skill | None:
        return self.skills.get(skill_name)

    @classmethod
    def get_instance(cls, logger: AgentLogger | None = None):
        if cls._instance is None:
            cls._instance = cls(logger=logger)
        elif logger is not None:
            cls._instance.set_logger(logger)
        return cls._instance

    # -- Discovery ----------------------------------------------------------

    def load_skills_from_directory(
        self,
        directory: str,
        platform: str | None = None,
        *,
        load_mode: str = "on-demand",
        allow_scripts: bool = True,
        allow_network: bool = True,
        enable_hooks: object = _UNSET,
        policy_priority: int = 0,
        policy_fields: set[str] | None = None,
    ) -> list[str]:
        """Load Claude-style skill packages from a directory tree.

        A skill package is a directory containing a ``SKILL.md`` entrypoint,
        matched case-insensitively for filesystem portability. Loose markdown
        files and plural aliases are intentionally not loaded.
        """
        _reject_legacy_hook_policy(enable_hooks, policy_fields)
        path = Path(directory)
        if not path.exists():
            self._logger.warning(f"Skills directory not found: {directory}")
            return []

        loaded_names = []
        for file_path in self.discover_skill_files(path):
            skill = self.load_skill_metadata(
                str(file_path),
                platform=platform,
                load_mode=load_mode,
                allow_scripts=allow_scripts,
                allow_network=allow_network,
                policy_priority=policy_priority,
                policy_fields=policy_fields,
            )
            if skill:
                loaded_names.append(skill.metadata.name)
        return loaded_names

    def discover_skill_files(self, path: Path) -> list[Path]:
        """Return skill entrypoint files for *path* in deterministic order."""
        path = path.resolve()
        if path.is_file():
            if _is_skill_entrypoint(path):
                return [path]
            self._logger.warning("Skills path is not a SKILL.md entrypoint file: %s", path)
            return []
        if not path.is_dir():
            self._logger.warning("Skills path is not a directory: %s", path)
            return []

        candidates: list[Path] = []
        seen: set[Path] = set()

        def _add(candidate: Path) -> None:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(resolved)

        def _walk(directory: Path) -> None:
            exact_skill_files = [
                child for child in directory.iterdir()
                if child.is_file() and _is_skill_entrypoint(child)
            ]
            if exact_skill_files:
                if len(exact_skill_files) > 1:
                    names = ", ".join(sorted(child.name for child in exact_skill_files))
                    raise ValueError(f"Ambiguous skill entrypoints in {directory}: {names}")
                _add(exact_skill_files[0])
                return

            for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir():
                    continue
                if child.name in _RESOURCE_DIR_EXCLUDES or child.name.startswith("."):
                    continue
                if _is_generated_proposal_path(child):
                    continue
                _walk(child)

        _walk(path)
        return sorted(candidates, key=lambda p: str(p))

    def load_skill_metadata(
        self,
        file_path: str,
        platform: str | None = None,
        *,
        load_mode: str = "on-demand",
        allow_scripts: bool = True,
        allow_network: bool = True,
        enable_hooks: object = _UNSET,
        policy_priority: int = 0,
        policy_fields: set[str] | None = None,
    ) -> Skill | None:
        """Load skill metadata only (no body content)."""
        try:
            _reject_legacy_hook_policy(enable_hooks, policy_fields)
            path = Path(file_path)
            if not _is_skill_entrypoint(path):
                raise ValueError(f"AgentLoom skills must be loaded from SKILL.md entrypoint files: {file_path}")

            metadata, _markdown_body = parse_skill_file(file_path, logger=self._logger)
            absolute_file_path = str(path.absolute())
            resolved_file_path = str(path.resolve())

            if platform:
                metadata.platform = platform
            self._apply_runtime_options(
                metadata,
                load_mode=load_mode,
                allow_scripts=allow_scripts,
                allow_network=allow_network,
                policy_priority=policy_priority,
                policy_fields=policy_fields,
            )
            self._apply_tools_mapping(metadata)

            existing = self.skills.get(metadata.name)
            if existing is not None:
                existing_path = str(Path(existing.file_path).resolve())
                if existing_path == resolved_file_path:
                    if policy_priority >= existing.metadata.policy_priority:
                        self._apply_runtime_options(
                            existing.metadata,
                            load_mode=load_mode,
                            allow_scripts=allow_scripts,
                            allow_network=allow_network,
                            policy_priority=policy_priority,
                            policy_fields=policy_fields,
                        )
                        if platform:
                            existing.metadata.platform = platform
                        # A later effective-config occurrence owns both policy
                        # and order. Lower-priority directory discovery cannot
                        # move an explicitly configured Skill.
                        self.skills.pop(metadata.name)
                        self.skills[metadata.name] = existing
                    return existing
                raise ValueError(
                    f"Duplicate skill name '{metadata.name}' loaded from '{existing_path}' and '{resolved_file_path}'"
                )

            skill = Skill(metadata=metadata, content=None, file_path=absolute_file_path)
            self.skills[metadata.name] = skill

            self._logger.info(f"Loaded skill metadata: {metadata.name} (Platform: {metadata.platform})")
            return skill
        except ValueError:
            raise
        except Exception as e:
            self._logger.error(f"Error loading skill from {file_path}: {e}")
            return None

    # -- Content loading ----------------------------------------------------

    def get_skill_content(self, name: str) -> SkillContent | None:
        """Load full skill content on demand without changing Hook Plan."""
        skill = self.skills.get(name)
        if skill is None:
            return None

        if skill.content is None:
            try:
                _metadata, markdown_body = parse_skill_file(skill.file_path, logger=self._logger)
                skill.content = markdown_body
                self._logger.info("Loaded skill body: %s", name)
            except Exception as e:
                self._logger.error(f"Error loading skill content for {name}: {e}")
                return None

        return SkillContent(metadata=skill.metadata, instructions=skill.content)

    # -- Resource helpers ---------------------------------------------------

    def get_skill_base_dir(self, name: str) -> Path | None:
        skill = self.skills.get(name)
        if skill is None:
            return None
        return Path(skill.file_path).resolve().parent

    def list_skill_resources(self, name: str) -> list[dict[str, Any]]:
        """List files bundled with a skill, relative to the skill directory."""
        base_dir = self.get_skill_base_dir(name)
        if base_dir is None or not base_dir.exists():
            return []
        resources: list[dict[str, Any]] = []
        for file_path in sorted(base_dir.rglob("*"), key=lambda p: str(p)):
            if not file_path.is_file():
                continue
            try:
                rel = file_path.relative_to(base_dir).as_posix()
            except ValueError:
                continue
            if _is_ignored_resource_path(rel):
                continue
            resources.append(
                {
                    "path": rel,
                    "bytes": file_path.stat().st_size,
                    "kind": _resource_kind(rel),
                }
            )
        return resources

    def read_skill_resource(
        self,
        name: str,
        resource_path: str,
        *,
        offset: int = 1,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read a text resource from inside a skill package."""
        base_dir = self.get_skill_base_dir(name)
        if base_dir is None:
            raise ValueError(f"Skill '{name}' not found")
        if not resource_path or not isinstance(resource_path, str):
            raise ValueError("path is required")

        candidate = (base_dir / resource_path).resolve()
        if candidate != base_dir and base_dir not in candidate.parents:
            raise ValueError(f"Skill resource path escapes skill directory: {resource_path!r}")
        try:
            rel = candidate.relative_to(base_dir).as_posix()
        except ValueError:
            rel = resource_path
        if _is_ignored_resource_path(rel):
            raise ValueError(f"Skill resource path is ignored dependency/cache content: {resource_path!r}")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"Skill resource not found: {resource_path}")

        raw = candidate.read_bytes()
        if b"\x00" in raw[:4096]:
            raise ValueError(f"Skill resource appears to be binary: {resource_path}")
        text = raw[: SKILL_RESOURCE_TEXT_MAX_CHARS + 1].decode("utf-8", errors="replace")
        truncated_by_bytes = len(raw) > SKILL_RESOURCE_TEXT_MAX_CHARS
        lines = text.splitlines()
        start = max(int(offset or 1), 1)
        max_lines = max(min(int(limit or 200), 1000), 1)
        selected = lines[start - 1 : start - 1 + max_lines]
        return {
            "skill": name,
            "path": resource_path,
            "absolute_path": str(candidate),
            "offset": start,
            "limit": max_lines,
            "total_lines": len(lines),
            "truncated_by_bytes": truncated_by_bytes,
            "content": "\n".join(selected),
        }

    # -- Dependency and script execution -----------------------------------

    def check_skill_dependencies(self, name: str) -> dict[str, Any]:
        skill = self.skills.get(name)
        if skill is None:
            raise ValueError(f"Skill '{name}' not found")

        base_dir = Path(skill.file_path).resolve().parent
        package_json = base_dir / "package.json"
        pyproject = base_dir / "pyproject.toml"
        requirements = base_dir / "requirements.txt"
        scripts_dir = base_dir / "scripts"

        bins: list[dict[str, Any]] = []
        _append_bin(bins, "node") if _needs_node(base_dir) else None
        _append_bin(bins, "python") if _needs_python(base_dir) else None
        _append_bin(bins, "sh") if _needs_shell(base_dir) else None

        package_dependency_count = _package_dependency_count(package_json)
        node_modules_present = (base_dir / "node_modules").exists()
        missing = [f"bin:{entry['name']}" for entry in bins if not entry["found"]]
        if package_dependency_count and not node_modules_present:
            missing.append("package:node_modules")

        return {
            "skill": name,
            "base_dir": str(base_dir),
            "bins": bins,
            "package_json": str(package_json) if package_json.exists() else None,
            "package_dependency_count": package_dependency_count,
            "node_modules_present": node_modules_present,
            "pyproject": str(pyproject) if pyproject.exists() else None,
            "requirements_txt": str(requirements) if requirements.exists() else None,
            "scripts_dir": str(scripts_dir) if scripts_dir.exists() else None,
            "ok": not missing,
            "missing": missing,
        }

    def run_skill_script(
        self,
        name: str,
        command: str,
        *,
        args: str = "",
        cwd: str = "skill",
        timeout: int = 60,
        env_allowlist: str = "",
        allow_network: bool = True,
    ) -> dict[str, Any]:
        """Run a command for a skill with an audit trail."""
        skill = self.skills.get(name)
        if skill is None:
            raise ValueError(f"Skill '{name}' not found")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")

        base_dir = Path(skill.file_path).resolve().parent
        workspace_dir = _skill_workspace_dir(name)

        cwd_normalized = (cwd or "skill").strip().lower()
        if cwd_normalized == "skill":
            run_cwd = base_dir
        elif cwd_normalized == "workspace":
            run_cwd = workspace_dir
        else:
            raise ValueError("cwd must be 'skill' or 'workspace'")

        timeout = max(1, min(int(timeout or 60), 600))
        final_command = _substitute_skill_placeholders(
            command.strip(),
            skill_dir=base_dir,
            workspace_dir=workspace_dir,
        )
        if args:
            final_command = f"{final_command} {args}"

        audit_dir = _new_skill_execution_dir(name)

        effective_allow_network = bool(allow_network) and bool(skill.metadata.allow_network)
        blocked_reason = None
        if not skill.metadata.allow_scripts:
            blocked_reason = "script execution is disabled by skill configuration"
        elif not effective_allow_network:
            blocked_reason = _blocked_network_reason(final_command)

        env = _skill_script_env(
            skill_dir=base_dir,
            workspace_dir=workspace_dir,
            env_allowlist=env_allowlist,
        )

        audit: dict[str, Any] = {
            "skill": name,
            "command": final_command,
            "cwd": str(run_cwd),
            "timeout": timeout,
            "allow_scripts": skill.metadata.allow_scripts,
            "allow_network": effective_allow_network,
            "env_names": sorted(env.keys()),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "audit_dir": str(audit_dir),
        }

        if blocked_reason:
            audit.update({"blocked": True, "blocked_reason": blocked_reason, "returncode": None})
            _write_audit(audit_dir, audit, "", "")
            return audit

        started = time.monotonic()
        capture: SkillSubprocessCapture | None = None
        process_result = None
        execution_error: Exception | None = None
        snapshot = SkillOutputSnapshot("", "", 0, 0, False, False)
        try:
            capture = SkillSubprocessCapture(audit_dir)
            process_result = run_skill_subprocess(
                final_command,
                cwd=str(run_cwd),
                env=env,
                stdout_fd=capture.stdout_fd,
                stderr_fd=capture.stderr_fd,
                timeout=timeout,
            )
        except Exception as exc:
            execution_error = exc
        finally:
            if capture is not None:
                try:
                    snapshot = capture.snapshot(
                        stdout_limit=OUTPUT_PREVIEW_MAX_BYTES,
                        stderr_limit=OUTPUT_PREVIEW_MAX_BYTES,
                    )
                finally:
                    capture.close()

        timed_out = process_result.timed_out if process_result is not None else False
        audit.update(
            {
                "blocked": False,
                "timed_out": timed_out,
                "returncode": process_result.returncode if process_result is not None else None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout_path": str(audit_dir / "stdout.txt"),
                "stderr_path": str(audit_dir / "stderr.txt"),
                "stdout_bytes": snapshot.stdout_bytes,
                "stderr_bytes": snapshot.stderr_bytes,
                "stdout_preview_truncated": snapshot.stdout_truncated,
                "stderr_preview_truncated": snapshot.stderr_truncated,
                "stdout_preview": snapshot.stdout,
                "stderr_preview": snapshot.stderr,
            }
        )
        if execution_error is not None:
            audit.update(
                {
                    "execution_error": type(execution_error).__name__,
                    "execution_error_message": str(execution_error),
                }
            )
        _write_audit_record(audit_dir, audit)
        if execution_error is not None:
            raise execution_error
        return audit

    # -- Eager support ------------------------------------------------------

    def get_eager_skill_names(self) -> set[str]:
        """Return skill names configured for eager full-context loading."""
        return {
            name for name, skill in self.skills.items()
            if skill.metadata.load_mode == "eager"
        }

    def get_eager_skills_prompt(self) -> str:
        """Build the system-prompt section for eagerly loaded skills."""
        eager_names = self.get_eager_skill_names()
        if not eager_names:
            return ""

        parts: list[str] = [
            "",
            "====",
            "",
            "EAGER LOADED SKILLS",
            "",
            "<eager_loaded_skills>",
            "<!-- The following skills have been pre-loaded into your context. -->",
            "<!-- Do NOT call load_skill for these skills; their instructions are already here. -->",
        ]

        for name in sorted(eager_names):
            content = self.get_skill_content(name)
            if content is None:
                self._logger.warning("Eager skill: failed to load content for skill '%s'", name)
                continue

            parts.append(f"\n<eager_loaded_skill name=\"{name}\">")
            if content.metadata.description:
                parts.append(f"<description>{content.metadata.description}</description>")
            if content.metadata.allowed_tools:
                parts.append("<allowed_tools>")
                for tool in content.metadata.allowed_tools:
                    parts.append(f"  <tool>{tool}</tool>")
                parts.append("</allowed_tools>")
            parts.append("<instructions>")
            parts.append(content.instructions)
            parts.append("</instructions>")
            parts.append("</eager_loaded_skill>")

            self._logger.info("Eager-loaded skill into system prompt: %s", name)

        parts.append("\n</eager_loaded_skills>")
        return "\n".join(parts)

    # -- Prompt generation --------------------------------------------------

    def get_skills_prompt(self) -> str:
        return build_skills_prompt(self.skills)

    # -- Tools mapping ------------------------------------------------------

    def _apply_runtime_options(
        self,
        metadata: SkillMetadata,
        *,
        load_mode: str,
        allow_scripts: bool,
        allow_network: bool,
        policy_priority: int,
        policy_fields: set[str] | None = None,
    ) -> None:
        normalized_mode = (load_mode or "on-demand").strip().lower()
        if normalized_mode not in {"on-demand", "eager"}:
            raise ValueError("skills.load-mode must be 'on-demand' or 'eager'")
        fields = (
            set(policy_fields)
            if policy_fields is not None
            else {"load_mode", "allow_scripts", "allow_network"}
        )
        if "load_mode" in fields:
            metadata.load_mode = normalized_mode
        if "allow_scripts" in fields:
            metadata.allow_scripts = bool(allow_scripts)
        if "allow_network" in fields:
            metadata.allow_network = bool(allow_network)
        metadata.policy_priority = int(policy_priority)

    def _apply_tools_mapping(self, metadata: SkillMetadata) -> None:
        """Remap allowed tool names for the target platform."""
        target_platform = metadata.platform or "Claude"
        if target_platform not in self._tools_mapping:
            return

        mapping = self._tools_mapping[target_platform]

        if metadata.allowed_tools:
            metadata.allowed_tools = [
                mapping.get(tool, tool) for tool in metadata.allowed_tools
            ]


def _reject_legacy_hook_policy(
    enable_hooks: object,
    policy_fields: set[str] | None,
) -> None:
    """Fail loudly when old Skill-owned Hook authorization is requested."""
    fields = policy_fields or set()
    if enable_hooks is not _UNSET or {"enable_hooks", "enable-hooks"} & fields:
        raise ValueError(
            "skills.enable-hooks is not supported; configure a direct Hook or "
            "standalone Hook Bundle instead"
        )


def _is_skill_entrypoint(path: Path) -> bool:
    return path.name.lower() == "skill.md"


def _is_generated_proposal_path(path: Path) -> bool:
    parts = path.parts
    for idx in range(len(parts) - 1):
        if parts[idx] == "generated" and parts[idx + 1] == "proposals":
            return True
    return False


def _is_ignored_resource_path(relative_path: str) -> bool:
    parts = Path(relative_path).parts
    if _is_generated_proposal_path(Path(relative_path)):
        return True
    return any(part in _RESOURCE_DIR_EXCLUDES for part in parts)


def _resource_kind(path: str) -> str:
    first = path.split("/", 1)[0]
    if first in {"references", "scripts", "assets", "evals", "agents"}:
        return first
    if _is_skill_entrypoint(Path(path)):
        return "skill"
    if path == "package.json":
        return "package"
    return "file"


def _append_bin(bins: list[dict[str, Any]], name: str) -> None:
    if any(entry["name"] == name for entry in bins):
        return
    resolved = shutil.which(name)
    bins.append({"name": name, "found": resolved is not None, "path": resolved})


def _package_dependency_count(package_json: Path) -> int:
    if not package_json.exists():
        return 0
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return 0
    count = 0
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            count += len(value)
    return count


def _needs_node(base_dir: Path) -> bool:
    if (base_dir / "package.json").exists():
        return True
    scripts_dir = base_dir / "scripts"
    if scripts_dir.exists():
        return any(path.suffix in {".js", ".mjs", ".cjs"} for path in scripts_dir.rglob("*") if path.is_file())
    return False


def _needs_python(base_dir: Path) -> bool:
    if (base_dir / "requirements.txt").exists() or (base_dir / "pyproject.toml").exists():
        return True
    scripts_dir = base_dir / "scripts"
    if scripts_dir.exists():
        for path in scripts_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix == ".py":
                return True
            try:
                first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            except Exception:
                first = ""
            if first.startswith("#!") and "python" in first:
                return True
    return False


def _needs_shell(base_dir: Path) -> bool:
    scripts_dir = base_dir / "scripts"
    if not scripts_dir.exists():
        return False
    for path in scripts_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".sh", ".bash", ".zsh"}:
            return True
        try:
            first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        except Exception:
            first = ""
        if first.startswith("#!") and any(shell in first for shell in ("sh", "bash", "zsh")):
            return True
    return False


def _skill_workspace_dir(skill_name: str) -> Path:
    from src.lib.runtime import get_current_run_context

    runtime_context = get_current_run_context(required=True)
    assert runtime_context is not None
    return runtime_context.prepare_skill_workspace(skill_name)


def _new_skill_execution_dir(skill_name: str) -> Path:
    from src.lib.runtime import get_current_run_context

    runtime_context = get_current_run_context(required=True)
    assert runtime_context is not None
    return runtime_context.new_skill_execution_dir(skill_name)


def _substitute_skill_placeholders(command: str, *, skill_dir: Path, workspace_dir: Path) -> str:
    replacements = {
        "{baseDir}": str(skill_dir),
        "${CLAUDE_SKILL_DIR}": str(skill_dir),
        "${AGENTLOOM_SKILL_DIR}": str(skill_dir),
        "${AGENTLOOM_SKILL_WORKSPACE}": str(workspace_dir),
    }
    try:
        from src.trace.task_context import get_current_task_id

        session_id = get_current_task_id() or ""
    except Exception:
        session_id = ""
    replacements["${CLAUDE_SESSION_ID}"] = session_id
    for key, value in replacements.items():
        command = command.replace(key, shlex.quote(value))
    return command


def _skill_script_env(
    *,
    skill_dir: Path,
    workspace_dir: Path,
    env_allowlist: str = "",
) -> dict[str, str]:
    names = _parse_env_allowlist(env_allowlist)
    env = (
        {name: os.environ[name] for name in names if name in os.environ}
        if names
        else dict(os.environ)
    )
    env.update(
        {
            "AGENTLOOM_SKILL_DIR": str(skill_dir),
            "CLAUDE_SKILL_DIR": str(skill_dir),
            "AGENTLOOM_SKILL_WORKSPACE": str(workspace_dir),
        }
    )
    try:
        from src.trace.task_context import get_current_task_id

        env["CLAUDE_SESSION_ID"] = get_current_task_id() or ""
    except Exception:
        env["CLAUDE_SESSION_ID"] = ""
    return env


def _parse_env_allowlist(value: str) -> set[str]:
    if not value or not isinstance(value, str):
        return set()
    return {
        item.strip()
        for item in re.split(r"[,|\s]+", value)
        if item.strip()
    }


def _blocked_network_reason(command: str) -> str | None:
    network_words = {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "ssh",
        "scp",
        "rsync",
        "npm",
        "pip",
        "pnpm",
        "yarn",
    }
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        command_name = Path(token).name
        if command_name in network_words:
            return f"network command '{command_name}' is blocked because allow-network=false"
    return None


def _write_audit(audit_dir: Path, audit: dict[str, Any], stdout: str, stderr: str) -> None:
    from src.lib.runtime import get_current_run_context

    runtime_context = get_current_run_context(required=True)
    assert runtime_context is not None
    runtime_context.atomic_write_run_file(audit_dir / "stdout.txt", stdout)
    runtime_context.atomic_write_run_file(audit_dir / "stderr.txt", stderr)
    runtime_context.atomic_write_run_file(
        audit_dir / "audit.json",
        json.dumps(audit, ensure_ascii=False, indent=2),
    )


def _write_audit_record(audit_dir: Path, audit: dict[str, Any]) -> None:
    from src.lib.runtime import get_current_run_context

    runtime_context = get_current_run_context(required=True)
    assert runtime_context is not None
    runtime_context.atomic_write_run_file(
        audit_dir / "audit.json",
        json.dumps(audit, ensure_ascii=False, indent=2),
    )
