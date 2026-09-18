"""Run one deterministic ToolCalling Application through every model protocol.

This is a manual real-provider acceptance runner, not an ordinary pytest test.
It reads the ignored ``config/llm.yaml`` but never modifies or copies it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

ROOT = Path(__file__).resolve().parents[2]
LLM_CONFIG_PATH = ROOT / "config" / "llm.yaml"
ADAPTERS = (
    "openai_chat",
    "openai_responses",
    "anthropic_messages",
)
ADAPTER_CREDENTIAL_ENV = {
    "openai_chat": (
        "OPENAI_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ),
    "openai_responses": (
        "OPENAI_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ),
    "anthropic_messages": ("ANTHROPIC_API_KEY",),
}
TOKEN = "AGENTLOOM_PROTOCOL_MATRIX_OK"
DEFAULT_TIMEOUT_SECONDS = 600
Status = Literal["PASSED", "FAILED", "NOT-RUN"]


@dataclass(frozen=True, slots=True)
class MatrixCase:
    adapter: str
    profile: str | None
    model: str | None
    runnable: bool
    reason: str | None = None


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()


def _configured_secret_values(config: object) -> tuple[str, ...]:
    values: set[str] = set()
    models = getattr(config, "models", {})
    for settings in models.values():
        for name in ("api_key", "base_url"):
            value = getattr(settings, name, None)
            if isinstance(value, str) and len(value.strip()) >= 4:
                values.add(value.strip())
    return tuple(sorted(values, key=len, reverse=True))


def redact_text(value: object, *, secrets: tuple[str, ...] = ()) -> str:
    """Remove configured secrets, URLs, bearer tokens, and common API-key forms."""

    text = str(value)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    text = re.sub(r"https?://[^\s'\"<>]+", "<redacted-url>", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|token|secret|password)"
        r"\s*[:=]\s*[^\s,;}]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[^\s,;}]+", "Bearer <redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted-key>", text)
    return text


def _is_configured_credential(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(normalized) and normalized not in {
        "not_provided",
        "none",
        "null",
        "xxxxxxx",
        "<redacted>",
    } and not normalized.startswith(("your-", "your_", "${"))


def _profile_has_credentials(settings: object, environ: dict[str, str]) -> bool:
    api_key = getattr(settings, "api_key", "")
    if isinstance(api_key, str) and _is_configured_credential(api_key):
        return True
    adapter = str(getattr(settings, "adapter", "") or "")
    credential_keys = ADAPTER_CREDENTIAL_ENV.get(adapter, ())
    if any(
        _is_configured_credential(environ.get(key, ""))
        for key in credential_keys
    ):
        return True
    base_url = str(getattr(settings, "base_url", "") or "").lower()
    model = str(getattr(settings, "model", "") or "").lower()
    return model.startswith(("ollama/", "local/")) or any(
        marker in base_url
        for marker in ("localhost", "127.0.0.1", "[::1]")
    )


def select_cases(
    config: object,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[MatrixCase, ...]:
    """Select one deterministic explicit profile for each adapter."""

    environment = dict(os.environ if environ is None else environ)
    models = dict(getattr(config, "models", {}))
    default_profile = str(getattr(config, "default_model_type", "") or "")
    cases: list[MatrixCase] = []
    for adapter in ADAPTERS:
        candidates = [
            (name, settings)
            for name, settings in models.items()
            if getattr(settings, "adapter", None) == adapter
            and str(getattr(settings, "model", "") or "").strip()
        ]
        candidates.sort(
            key=lambda item: (
                item[0] != default_profile,
                item[0] == "summary",
                item[0],
            )
        )
        if not candidates:
            cases.append(
                MatrixCase(
                    adapter=adapter,
                    profile=None,
                    model=None,
                    runnable=False,
                    reason=f"no explicit config/llm.yaml profile declares adapter={adapter}",
                )
            )
            continue
        runnable = [
            item
            for item in candidates
            if _profile_has_credentials(item[1], environment)
        ]
        profile, settings = runnable[0] if runnable else candidates[0]
        model = str(settings.model)
        cases.append(
            MatrixCase(
                adapter=adapter,
                profile=profile,
                model=model,
                runnable=bool(runnable),
                reason=(
                    None
                    if runnable
                    else "selected profile has no configured or recognized provider credential"
                ),
            )
        )
    return tuple(cases)


def _load_config() -> object:
    from agentloom.configuration.llm_config import LLMConfig

    if not LLM_CONFIG_PATH.is_file():
        raise FileNotFoundError("ignored config/llm.yaml is absent")
    return LLMConfig.load_from_yaml(LLM_CONFIG_PATH)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _workflow(profile: str) -> dict[str, object]:
    return {
        "name": "model_protocol_matrix",
        "agent_runtime": "smolagents",
        "description": "Validate one configured model wire protocol.",
        "model_type": profile,
        "max_steps": 4,
        "todo": {"mode": "off"},
        "checkpoint": {"enabled": True, "cleanup_on_success": False},
        "workflow": (
            "Use native structured tool calls only. "
            f"First call protocol_matrix_echo with token exactly {TOKEN!r}. "
            "Verify the actual Tool result equals that token. "
            f"Then call final_answer with answer exactly {TOKEN!r}. "
            "Do not return ordinary assistant text instead of either Tool call."
        ),
        "tools": [
            {
                "name": "protocol_matrix_echo",
                "module": "tests.acceptance.protocol_matrix_tool",
                "function": "protocol_matrix_echo",
            }
        ],
        "worker_agents": [],
    }


def application_workflow_path(workspace: Path, adapter: str) -> Path:
    """Return a unique, discoverable Application workflow path."""

    digest = hashlib.sha256(
        f"{workspace.resolve()}:{adapter}".encode()
    ).hexdigest()[:12]
    return (
        ROOT
        / "applications"
        / f"architecture_acceptance_protocol_{adapter}_{digest}"
        / "workflows"
        / "protocol_matrix.yaml"
    )


def _checkpoint_path(run: object, *, runtime_root: Path) -> Path:
    return (
        runtime_root
        / "checkpoints"
        / Path(*str(run.application_id).split("/"))
        / str(run.task_id)
        / "checkpoint.json"
    )


def _validate_checkpoint(
    checkpoint_path: Path,
    *,
    task_id: str,
    run_id: str,
    adapter_id: str,
) -> dict[str, object]:
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    envelope = checkpoint.get("runtime_checkpoint")
    if not isinstance(envelope, dict):
        raise AssertionError("checkpoint lacks runtime_checkpoint")
    if envelope.get("runtime_id") != "smolagents":
        raise AssertionError("checkpoint did not use smolagents runtime")
    if envelope.get("state_schema_version") != 2:
        raise AssertionError("checkpoint did not use state schema 2")
    if not isinstance(envelope.get("runtime_version"), str) or not envelope[
        "runtime_version"
    ]:
        raise AssertionError("checkpoint lacks runtime_version")
    audit_metadata = envelope.get("audit_metadata")
    if (
        not isinstance(audit_metadata, dict)
        or audit_metadata.get("model_adapter_id") != adapter_id
    ):
        raise AssertionError(
            "checkpoint model_adapter_id does not match the selected adapter"
        )
    if envelope.get("task_id") != task_id or envelope.get("run_id") != run_id:
        raise AssertionError("checkpoint identity does not match public Run")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise AssertionError("checkpoint payload is not an object")
    steps = payload.get("memory_steps")
    canonical = payload.get("canonical_model_items")
    if not isinstance(steps, list) or not isinstance(canonical, list):
        raise AssertionError(
            "checkpoint lacks memory_steps or canonical_model_items"
        )
    records = [
        record
        for step in steps
        if isinstance(step, dict)
        for record in step.get("tool_results") or []
        if isinstance(record, dict)
    ]
    completed = {
        (record.get("call_id"), record.get("tool_name"))
        for record in records
        if record.get("status") == "completed"
    }
    echo_records = [
        record
        for record in records
        if record.get("tool_name") == "protocol_matrix_echo"
        and record.get("status") == "completed"
        and record.get("output") == TOKEN
    ]
    final_records = [
        record
        for record in records
        if record.get("tool_name") == "final_answer"
        and record.get("status") == "completed"
        and record.get("output") == TOKEN
    ]
    if len(echo_records) != 1 or len(final_records) != 1:
        raise AssertionError("checkpoint lacks completed echo/final_answer records")
    canonical_items = [
        entry.get("item")
        for entry in canonical
        if isinstance(entry, dict) and isinstance(entry.get("item"), dict)
    ]
    calls = {
        (item.get("call_id"), item.get("name"))
        for item in canonical_items
        if item.get("type") == "function_call"
    }
    outputs = {
        item.get("call_id")
        for item in canonical_items
        if item.get("type") == "function_call_output"
    }
    for call_id, tool_name in completed:
        if tool_name in {"protocol_matrix_echo", "final_answer"} and (
            (call_id, tool_name) not in calls or call_id not in outputs
        ):
            raise AssertionError(
                f"canonical stream lacks correlated {tool_name} call/output"
            )
    return {
        "path": str(checkpoint_path),
        "sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
        "runtime_id": envelope["runtime_id"],
        "runtime_version": envelope.get("runtime_version"),
        "state_schema_version": envelope["state_schema_version"],
        "memory_step_count": len(steps),
        "canonical_item_count": len(canonical),
        "tool_records": [
            {
                "call_id": record["call_id"],
                "tool_name": record["tool_name"],
                "status": record["status"],
            }
            for record in (*echo_records, *final_records)
        ],
    }


def run_case(case: MatrixCase, workspace: Path) -> dict[str, object]:
    """Run one real profile and return only allowlisted, redacted evidence."""

    if not case.runnable or case.profile is None or case.model is None:
        return {
            "revision": _revision(),
            "adapter": case.adapter,
            "profile": case.profile,
            "model": case.model,
            "status": "NOT-RUN",
            "reason": case.reason,
        }
    from agentloom.application.runner import execute_app
    from agentloom.configuration import C

    workspace.mkdir(parents=True, exist_ok=True)
    workflow = application_workflow_path(workspace, case.adapter)
    workflow.parent.mkdir(parents=True, exist_ok=False)
    workflow.write_text(
        yaml.safe_dump(_workflow(case.profile), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    C.raw.setdefault("runtime", {})["root_dir"] = str(workspace / "runtime")
    C.raw.setdefault("checkpoint", {}).update(
        {"enabled": True, "cleanup_on_success": False}
    )
    C.raw.setdefault("lsp_servers", {})["enabled"] = False
    C.raw["skills"] = {"paths": []}
    C.raw.setdefault("logging", {})["console_enabled"] = False
    lifecycle: list[dict[str, object]] = []

    def observe(event: object) -> None:
        lifecycle.append(asdict(event))

    started_at = datetime.now(UTC).isoformat()
    result = execute_app(workflow, file_logging=False, event_sink=observe)
    ended_at = datetime.now(UTC).isoformat()
    if result.output != TOKEN:
        raise AssertionError(f"final output differs from expected token: {result.output!r}")
    manifest = json.loads(result.run.manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "completed"
        or manifest.get("task_events_complete") is not True
        or manifest.get("application_id") != result.run.application_id
        or manifest.get("task_id") != result.run.task_id
        or manifest.get("run_id") != result.run.run_id
    ):
        raise AssertionError("manifest status or identity does not match public Run")
    event_names = [event.get("event") for event in lifecycle]
    if event_names != ["run.started", "run.completed"]:
        raise AssertionError(f"unexpected lifecycle sequence: {event_names}")
    checkpoint = _validate_checkpoint(
        _checkpoint_path(result.run, runtime_root=workspace / "runtime"),
        task_id=result.run.task_id,
        run_id=result.run.run_id,
        adapter_id=case.adapter,
    )
    _write_json(workspace / "lifecycle.json", lifecycle)
    return {
        "revision": _revision(),
        "adapter": case.adapter,
        "profile": case.profile,
        "model": case.model,
        "status": "PASSED",
        "started_at": started_at,
        "ended_at": ended_at,
        "run": {
            "application_id": result.run.application_id,
            "task_id": result.run.task_id,
            "run_id": result.run.run_id,
        },
        "manifest": {
            "path": str(result.run.manifest_path),
            "sha256": hashlib.sha256(
                result.run.manifest_path.read_bytes()
            ).hexdigest(),
            "status": manifest["status"],
        },
        "lifecycle": {
            "path": str(workspace / "lifecycle.json"),
            "events": event_names,
        },
        "checkpoint": checkpoint,
    }


def not_run_reports(reason: str) -> list[dict[str, object]]:
    return [
        {
            "revision": _revision(),
            "adapter": adapter,
            "profile": None,
            "model": None,
            "status": "NOT-RUN",
            "reason": reason,
        }
        for adapter in ADAPTERS
    ]


def matrix_exit_code(reports: list[dict[str, object]]) -> int:
    return 1 if any(report.get("status") == "FAILED" for report in reports) else 0


def _child(adapter: str, profile: str, workspace: Path) -> int:
    workspace.mkdir(parents=True, exist_ok=False)
    secrets: tuple[str, ...] = ()
    model: str | None = None
    try:
        config = _load_config()
        secrets = _configured_secret_values(config)
        settings = config.models[profile]
        if settings.adapter != adapter:
            raise ValueError("selected profile adapter changed before child execution")
        model = str(settings.model)
        case = MatrixCase(
            adapter=adapter,
            profile=profile,
            model=model,
            runnable=True,
        )
        report = run_case(case, workspace)
    except BaseException as exc:
        report = {
            "revision": _revision(),
            "adapter": adapter,
            "profile": profile,
            "model": model,
            "status": "FAILED",
            "reason": redact_text(
                f"{type(exc).__name__}: {exc}",
                secrets=secrets,
            ),
        }
    _write_json(workspace / "result.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return matrix_exit_code([report])


def _run_child(
    case: MatrixCase,
    workspace: Path,
    *,
    timeout_seconds: int,
    secrets: tuple[str, ...],
) -> dict[str, object]:
    assert case.profile is not None
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-adapter",
            case.adapter,
            "--child-profile",
            case.profile,
            "--workspace",
            str(workspace),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    workspace.mkdir(parents=True, exist_ok=True)
    process_log = redact_text(
        process.stdout + process.stderr,
        secrets=secrets,
    )
    (workspace / "process.log").write_text(process_log, encoding="utf-8")
    result_path = workspace / "result.json"
    if result_path.is_file():
        report = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        report = {
            "revision": _revision(),
            "adapter": case.adapter,
            "profile": case.profile,
            "model": case.model,
            "status": "FAILED",
            "reason": (
                f"child exited {process.returncode} without result evidence; "
                "inspect sanitized process.log"
            ),
        }
    if process.returncode != 0 and report.get("status") != "FAILED":
        report["status"] = "FAILED"
        report["reason"] = f"child exited with status {process.returncode}"
    return report


def execute_matrix(
    cases: tuple[MatrixCase, ...],
    root: Path,
    *,
    selected: tuple[str, ...],
    timeout_seconds: int,
    secrets: tuple[str, ...],
    runner: Callable[..., dict[str, object]] = _run_child,
) -> list[dict[str, object]]:
    """Execute independent adapter cases without fail-fast behavior."""

    reports: list[dict[str, object]] = []
    for case in cases:
        if case.adapter not in selected:
            continue
        if not case.runnable:
            reports.append(
                {
                    "revision": _revision(),
                    "adapter": case.adapter,
                    "profile": case.profile,
                    "model": case.model,
                    "status": "NOT-RUN",
                    "reason": case.reason,
                }
            )
            continue
        try:
            report = runner(
                case,
                root / case.adapter,
                timeout_seconds=timeout_seconds,
                secrets=secrets,
            )
        except subprocess.TimeoutExpired:
            report = {
                "revision": _revision(),
                "adapter": case.adapter,
                "profile": case.profile,
                "model": case.model,
                "status": "FAILED",
                "reason": f"real protocol run exceeded {timeout_seconds} seconds",
            }
        except Exception as exc:
            report = {
                "revision": _revision(),
                "adapter": case.adapter,
                "profile": case.profile,
                "model": case.model,
                "status": "FAILED",
                "reason": redact_text(
                    f"{type(exc).__name__}: {exc}",
                    secrets=secrets,
                ),
            }
        reports.append(report)
        _write_json(root / "summary.json", reports)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        help="New evidence directory; existing directories are never deleted",
    )
    parser.add_argument(
        "--adapter",
        choices=[*ADAPTERS, "all"],
        default="all",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--child-adapter", choices=ADAPTERS, help=argparse.SUPPRESS)
    parser.add_argument("--child-profile", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_adapter:
        if not args.child_profile or args.workspace is None:
            parser.error("child execution requires --child-profile and --workspace")
        return _child(args.child_adapter, args.child_profile, args.workspace)
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    root = args.workspace or (
        Path(tempfile.mkdtemp(prefix="agentloom-protocol-matrix-")) / "matrix"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        config = _load_config()
    except Exception as exc:
        reports = not_run_reports(
            redact_text(f"model configuration unavailable: {type(exc).__name__}: {exc}")
        )
        _write_json(root / "summary.json", reports)
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0

    secrets = _configured_secret_values(config)
    selected = (
        ADAPTERS
        if args.adapter == "all"
        else (args.adapter,)
    )
    reports = execute_matrix(
        select_cases(config),
        root,
        selected=selected,
        timeout_seconds=args.timeout,
        secrets=secrets,
    )
    for report in reports:
        print(json.dumps(report, ensure_ascii=False), flush=True)
    _write_json(root / "summary.json", reports)
    return matrix_exit_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
