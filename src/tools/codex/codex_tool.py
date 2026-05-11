"""AgentLoom tool wrapper for local ``codex exec``."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.lib.config import C
from src.lib.logging import get_logger

_VALID_SANDBOXES = {"", "read-only", "workspace-write", "danger-full-access"}
_DEFAULT_TIMEOUT_SECONDS = 600
_DEFAULT_MAX_OUTPUT_CHARS = 20000
logger = get_logger(__name__)


@dataclass(frozen=True)
class CodexExecSettings:
    """Internal runtime defaults for the local Codex exec tool."""

    timeout: int = _DEFAULT_TIMEOUT_SECONDS
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS


def codex(
    prompt: str,
    cwd: str = ".",
    model: str = "",
    timeout: str = "",
    sandbox: str = "",
    search: str = "",
) -> str:
    """Run local Codex CLI non-interactively through ``codex exec``.

    Args:
        prompt: Instructions to send to Codex.
        cwd: Working directory for Codex. Relative paths resolve from the AgentLoom root.
        model: Optional Codex model override. Empty string uses Codex local configuration.
        timeout: Optional execution timeout in seconds. Empty string uses the tool default.
        sandbox: Optional Codex sandbox mode: ``read-only``, ``workspace-write``, or
            ``danger-full-access``. Empty string does not pass ``--sandbox``.
        search: Optional web-search toggle. ``true`` passes Codex's global ``--search`` flag;
            empty string or ``false`` does not pass ``--search``.
            The wrapper never passes ``--ask-for-approval``; approval behavior follows the
            local Codex CLI configuration.

    Returns:
        JSON string with ``success``, ``summary``, ``output``, ``logs``, ``error``, and ``metadata``.
    """
    result = CodexExecRunner().run(
        prompt=prompt,
        cwd=cwd,
        model=model,
        timeout=timeout,
        sandbox=sandbox,
        search=search,
    )
    return json.dumps(result, ensure_ascii=False)


class CodexExecRunner:
    """Executes local ``codex exec`` and adapts results to AgentLoom's tool result shape."""

    def __init__(self, settings: CodexExecSettings | None = None, codex_binary: str = "codex"):
        self.settings = settings or _load_settings()
        self.codex_binary = codex_binary

    def run(
        self,
        prompt: str,
        cwd: str = ".",
        model: str = "",
        timeout: str | int = "",
        sandbox: str = "",
        search: str | bool = "",
    ) -> dict[str, Any]:
        start = time.monotonic()
        metadata: dict[str, Any] = {"runtime": "codex-exec", "truncated": False}

        if not isinstance(prompt, str) or not prompt.strip():
            return _failure("InvalidPrompt", "prompt must be a non-empty string", start, metadata=metadata)

        codex_path = shutil.which(self.codex_binary)
        if codex_path is None:
            return _failure(
                "RuntimeNotFound",
                "Codex CLI was not found on PATH",
                start,
                metadata=metadata,
            )

        cwd_result = _resolve_cwd(cwd)
        if cwd_result["error"]:
            return _failure("CwdRejected", cwd_result["error"], start, metadata=metadata)
        resolved_cwd = cwd_result["path"]
        metadata["cwd"] = str(resolved_cwd)

        effective_sandbox = _resolve_sandbox(sandbox)
        if effective_sandbox is None:
            return _failure(
                "InvalidSandbox",
                f"sandbox must be one of: {', '.join(sorted(v for v in _VALID_SANDBOXES if v))}",
                start,
                metadata=metadata,
            )
        metadata["sandbox"] = effective_sandbox

        effective_search = _resolve_search(search)
        if effective_search is None:
            return _failure(
                "InvalidSearchFlag",
                "search must be true, false, or an empty string",
                start,
                metadata=metadata,
            )
        metadata["search"] = effective_search

        effective_timeout = _resolve_timeout(timeout, self.settings.timeout)
        if effective_timeout is None:
            return _failure("InvalidTimeout", "timeout must be a positive integer", start, metadata=metadata)
        metadata["timeout"] = effective_timeout

        version_result = _run_checked([codex_path, "--version"], timeout=15, cwd=resolved_cwd)
        if version_result["returncode"] != 0:
            return _failure(
                "RuntimeNotFound",
                _combined_output(version_result) or "Unable to run codex --version",
                start,
                logs=_combined_output(version_result),
                metadata=metadata,
            )
        metadata["codex_version"] = version_result["stdout"].strip()

        login_result = _run_checked([codex_path, "login", "status"], timeout=15, cwd=resolved_cwd)
        if login_result["returncode"] != 0:
            return _failure(
                "AuthRequired",
                "Codex login status check failed",
                start,
                logs=_combined_output(login_result),
                metadata=metadata,
            )

        effective_model = _coerce_str(model)
        if effective_model:
            metadata["model"] = effective_model

        command = self._build_command(
            codex_path=codex_path,
            cwd=resolved_cwd,
            model=effective_model,
            sandbox=effective_sandbox,
            search=effective_search,
        )
        metadata["command"] = _redacted_command(command)
        logger.info(
            "Running local codex exec: cwd=%s sandbox=%s search=%s timeout=%ss",
            resolved_cwd,
            effective_sandbox or "<codex-default>",
            effective_search,
            effective_timeout,
        )

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                cwd=str(resolved_cwd),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logs = "\n".join(part for part in [exc.stdout or "", exc.stderr or ""] if part)
            logger.warning(
                "Local codex exec timed out after %ss: cwd=%s",
                effective_timeout,
                resolved_cwd,
            )
            return _failure(
                "TimeoutError",
                f"Codex execution exceeded timeout {effective_timeout}s",
                start,
                logs=logs,
                metadata={**metadata, "exit_code": None},
            )

        metadata["exit_code"] = completed.returncode

        stdout = _coerce_str(completed.stdout)
        stderr = _coerce_str(completed.stderr)

        output = _extract_output(stdout)
        logs = stderr

        if completed.returncode != 0:
            logger.warning(
                "Local codex exec failed with exit_code=%s: cwd=%s",
                completed.returncode,
                resolved_cwd,
            )
            return _failure(
                "ExecutionError",
                f"codex exec exited with code {completed.returncode}",
                start,
                output=output,
                logs=logs or stdout,
                metadata=metadata,
            )

        output, truncated = _truncate_output(output, self.settings.max_output_chars)
        metadata["truncated"] = truncated
        return {
            "success": True,
            "summary": _summarize_success(output),
            "output": output,
            "logs": logs,
            "error": None,
            "metadata": _finalize_metadata(start, metadata),
        }

    def _build_command(
        self,
        *,
        codex_path: str,
        cwd: Path,
        model: str,
        sandbox: str,
        search: bool,
    ) -> list[str]:
        command = [codex_path]
        if search:
            command.append("--search")
        command.extend(["exec", "--cd", str(cwd), "--json"])
        if model:
            command.extend(["--model", model])
        if sandbox:
            command.extend(["--sandbox", sandbox])
        command.append("-")
        return command


def _load_settings() -> CodexExecSettings:
    return CodexExecSettings()


def _resolve_cwd(raw_cwd: str) -> dict[str, Any]:
    raw = _coerce_str(raw_cwd) or "."
    path = Path(raw)
    if not path.is_absolute():
        path = Path(C.agent_root) / path
    try:
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        return {"path": None, "error": f"cwd resolution failed: {exc}"}
    if not resolved.exists():
        return {"path": None, "error": f"cwd does not exist: {resolved}"}
    if not resolved.is_dir():
        return {"path": None, "error": f"cwd is not a directory: {resolved}"}
    return {"path": resolved, "error": ""}


def _resolve_sandbox(raw: str) -> str | None:
    value = _coerce_str(raw)
    if value not in _VALID_SANDBOXES:
        return None
    return value


def _resolve_search(raw: str | bool) -> bool | None:
    if raw == "":
        return False
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value == "":
            return False
        if value == "true":
            return True
        if value == "false":
            return False
    return None


def _resolve_timeout(raw: str | int, configured: int) -> int | None:
    if raw == "":
        return configured
    return _coerce_positive_int(raw, 0) or None


def _run_checked(command: list[str], *, timeout: int, cwd: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd),
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


def _extract_output(stdout: Any) -> str:
    stdout = _coerce_str(stdout)
    last_text = ""
    raw_lines: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        raw_lines.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            last_text = line
            continue
        text = _extract_text_from_event(event)
        if text:
            last_text = text
    if last_text:
        return last_text
    return "\n".join(raw_lines).strip()


def _extract_text_from_event(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    for key in ("message", "output", "text", "content", "last_message", "summary"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("msg", "item", "data"):
        value = event.get(key)
        if isinstance(value, dict):
            nested = _extract_text_from_event(value)
            if nested:
                return nested
    return ""


def _failure(
    error_type: str,
    message: str,
    start: float,
    *,
    output: str = "",
    logs: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_metadata = _finalize_metadata(start, metadata or {})
    return {
        "success": False,
        "summary": message,
        "output": output,
        "logs": logs,
        "error": {"type": error_type, "message": message},
        "metadata": final_metadata,
    }


def _finalize_metadata(start: float, metadata: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(metadata)
    finalized["duration_ms"] = int((time.monotonic() - start) * 1000)
    return finalized


def _combined_output(result: dict[str, Any]) -> str:
    return "\n".join(
        part.strip()
        for part in [str(result.get("stdout", "")), str(result.get("stderr", ""))]
        if part and str(part).strip()
    )


def _truncate_output(output: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(output) <= max_chars:
        return output, False
    return output[:max_chars], True


def _summarize_success(output: Any) -> str:
    output = _coerce_str(output)
    if not output:
        return "Codex execution completed with no output"
    first_line = output.splitlines()[0]
    return first_line[:160]


def _redacted_command(command: list[str]) -> list[str]:
    return [str(part) for part in command]


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    if value is None:
        return default
    return bool(value)


def _coerce_positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default
