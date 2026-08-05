"""Persist and verify real-LLM Goal Mode validation evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _APP_ROOT.parents[1]
_OUTPUT_ROOT = _APP_ROOT / "outputs"
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_AUDIT_WORKERS = {
    "contract": "contract_auditor.yaml",
    "lifecycle": "lifecycle_adversary.yaml",
    "observability": "observability_auditor.yaml",
    "documentation": "documentation_auditor.yaml",
}
_EVIDENCE_FILES = {
    "contract": (
        "src/lib/goal/model.py",
        "src/lib/goal/provider.py",
        "src/lib/smolagents/agent/runtime_validation.py",
        "src/lib/smolagents/agent/yaml_agent_factory.py",
        "tests/goal_test/test_goal_config.py",
        "tests/goal_test/test_goal_state.py",
        "tests/agent_test/llm_cfg_test/test_supervisor_task_spec_format.py",
        "tests/agent_test/runtime_builder_test/test_runtime_builder.py",
    ),
    "lifecycle": (
        "src/lib/goal/model.py",
        "src/lib/goal/provider.py",
        "src/lib/smolagents/agent/base_agent.py",
        "src/lib/checkpoint/checkpoint_manager.py",
        "src/lib/checkpoint/coordinator.py",
        "src/runner.py",
        "tests/goal_test/test_goal_model_accounting.py",
        "tests/agent_test/runtime_builder_test/test_runtime_builder.py",
    ),
    "observability": (
        "src/application_run.py",
        "src/runner.py",
        "src/schedules/runner.py",
        "src/schedules/store.py",
        "src/tui_bridge/bridge.py",
        "src/tui_bridge/domain_cli.py",
        "agentloom-tui/src/app/presentation.ts",
        "agentloom-tui/src/domain/status.ts",
        "tests/schedules_test/test_schedule_runner_cli.py",
        "tests/tui_bridge_test/test_runtime_summary.py",
        "agentloom-tui/test/app/view.test.tsx",
    ),
    "documentation": (
        "README.md",
        "docs/cn/goal_mode.md",
        "docs/en/goal_mode.md",
        "docs/cn/agent_config.md",
        "docs/en/agent_config.md",
        "docs/cn/checkpoint.md",
        "docs/en/checkpoint.md",
        "agentloom-framework-skill/references/yaml-contract.md",
        "agentloom-framework-skill/references/validation-and-review.md",
    ),
}
_EVIDENCE_PATTERN = re.compile(
    r"goal|budget_limited|token_budget|workflow|continu|resume|checkpoint|"
    r"supervisor|worker|manifest|schedule|tui|jsonl|目标|预算|恢复",
    re.IGNORECASE,
)
_EVIDENCE_LINES_PER_FILE = 4


def _evenly_sample(values: list[int], limit: int) -> list[int]:
    if len(values) <= limit:
        return values
    return [values[index * (len(values) - 1) // (limit - 1)] for index in range(limit)]


def load_goal_audit_evidence(topic: str) -> str:
    """Load a bounded, line-numbered evidence bundle for one audit topic.

    Args:
        topic: One of contract, lifecycle, observability, or documentation.
    """

    files = _EVIDENCE_FILES.get(topic)
    if files is None:
        raise ValueError(
            "topic must be contract, lifecycle, observability, or documentation"
        )
    records: list[dict[str, object]] = []
    for relative in files:
        path = _REPO_ROOT / relative
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        selected: set[int] = set()
        for index, line in enumerate(lines):
            if _EVIDENCE_PATTERN.search(line):
                selected.add(index)
        ordered = _evenly_sample(sorted(selected), _EVIDENCE_LINES_PER_FILE)
        excerpts = [f"{index + 1}: {lines[index]}" for index in ordered]
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "excerpts": excerpts,
                "truncated": len(selected) > len(ordered),
            }
        )
    return json.dumps(
        {"topic": topic, "repository_root": str(_REPO_ROOT), "files": records},
        ensure_ascii=False,
    )


def run_goal_audit_batch(
    worker_name: str,
    tasks_json: str,
    concurrency: int = 1,
) -> str:
    """Run a validated real-LLM audit batch through AgentLoom's parallel API.

    Args:
        worker_name: One of contract, lifecycle, observability, or documentation.
        tasks_json: JSON array of objects containing unique task_id and query strings.
        concurrency: Positive Worker concurrency for this batch.
    """

    filename = _AUDIT_WORKERS.get(worker_name)
    if filename is None:
        raise ValueError(f"unsupported audit worker: {worker_name}")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 10
    ):
        raise ValueError("concurrency must be an integer from 1 through 10")
    tasks = json.loads(tasks_json)
    if not isinstance(tasks, list) or not tasks or len(tasks) > 20:
        raise ValueError("tasks_json must contain 1 through 20 tasks")
    task_ids: set[str] = set()
    normalized: list[dict[str, str]] = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {"task_id", "query"}:
            raise ValueError("each audit task must contain only task_id and query")
        task_id = task["task_id"]
        query = task["query"]
        if (
            not isinstance(task_id, str)
            or not _SAFE_NAME.fullmatch(task_id)
            or task_id in task_ids
            or not isinstance(query, str)
            or not 20 <= len(query.strip()) <= 8000
        ):
            raise ValueError("audit task_id/query is invalid or duplicated")
        task_ids.add(task_id)
        evidence = load_goal_audit_evidence(worker_name)
        normalized.append(
            {
                "task_id": task_id,
                "query": (
                    f"{query.strip()}\n\n"
                    "Use only the following read-only, line-numbered evidence bundle. "
                    "Do not call tools or inspect other files.\n"
                    f"EVIDENCE_BUNDLE={evidence}"
                ),
            }
        )

    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

    worker_path = _APP_ROOT / "workflows" / "worker_agents" / filename
    results = YamlAgentFactory.run_agents_parallel(
        worker_path,
        normalized,
        max_workers=concurrency,
    )
    return json.dumps(
        [
            {
                "task_id": result.task_id,
                "status": result.status,
                "duration_seconds": round(result.duration_seconds, 3),
                "result": result.result,
                "error": result.error,
            }
            for result in results
        ],
        ensure_ascii=False,
    )


def run_parallel_goal_budget_probe(tasks_json: str, concurrency: int = 6) -> str:
    """Run parallel contract Workers, persist evidence, then enforce the Goal fence.

    Args:
        tasks_json: JSON array of contract-audit task objects.
        concurrency: Positive Worker concurrency for this batch.
    """

    from src.lib.goal import get_current_goal_provider

    provider = get_current_goal_provider(required=True)
    existing = json.loads(inspect_parallel_goal_budget_report())
    if existing["matches_current_goal"]:
        return json.dumps(
            {
                "status": "existing_report",
                "batch_rerun": False,
                "report": existing,
                "goal": provider.snapshot().to_dict(),
            },
            ensure_ascii=False,
        )

    batch_json = run_goal_audit_batch(
        "contract",
        tasks_json,
        concurrency,
    )
    results = json.loads(batch_json)
    lines = [
        "# Parallel Goal Budget",
        "",
        "## Batch Results",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"- **{result['task_id']}**: status={result['status']}, "
                f"duration_seconds={result['duration_seconds']}",
                f"  Result: {result.get('result') or result.get('error') or 'none'}",
                "",
            ]
        )
    state = provider.snapshot()
    lines.extend(
        [
            "## Accounting",
            "",
            f"goal_id={state.goal_id}",
            f"used_tokens={state.used_tokens}",
            f"token_budget={state.token_budget}",
            f"status={state.status}",
            "",
            "## Resume Instructions",
            "",
            "Increase or remove token_budget, resume the same task_id, verify this report, and complete the Goal without rerunning the batch.",
        ]
    )
    persisted = persist_goal_validation_report(
        "parallel_budget",
        "\n".join(lines),
    )
    # This is intentionally after the durable report write. If the shared
    # Worker tree crossed the soft limit, raise before model code can claim
    # completion in the same in-flight Supervisor response.
    provider.assert_request_allowed()
    return json.dumps(
        {
            "status": "within_budget",
            "batch": results,
            "report": json.loads(persisted),
            "goal": provider.snapshot().to_dict(),
        },
        ensure_ascii=False,
    )


def inspect_parallel_goal_budget_report() -> str:
    """Report whether persisted parallel evidence belongs to the current Goal."""

    from src.lib.goal import get_current_goal_provider

    state = get_current_goal_provider(required=True).snapshot()
    target = _OUTPUT_ROOT / "parallel_budget.md"
    required = [
        "# Parallel Goal Budget",
        "## Batch Results",
        "## Accounting",
        "## Resume Instructions",
        f"goal_id={state.goal_id}",
    ]
    try:
        content = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    missing = [marker for marker in required if marker not in content]
    return json.dumps(
        {
            "matches_current_goal": not missing,
            "goal_id": state.goal_id,
            "path": str(target),
            "missing_markers": missing,
        },
        ensure_ascii=False,
    )


def persist_goal_validation_report(run_name: str, report: str) -> str:
    """Atomically persist one bounded Goal validation report.

    Args:
        run_name: Stable lowercase validation scenario name.
        report: Markdown or JSON evidence produced by the audit workers.
    """

    if not _SAFE_NAME.fullmatch(run_name):
        raise ValueError("run_name must be a safe lowercase identifier")
    report = str(report).strip()
    if len(report) < 80:
        raise ValueError("validation report is too short to be evidence")
    _OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    target = _OUTPUT_ROOT / f"{run_name}.md"
    temporary = _OUTPUT_ROOT / f".{run_name}.tmp"
    temporary.write_text(report + "\n", encoding="utf-8")
    temporary.replace(target)
    return json.dumps(
        {
            "status": "persisted",
            "path": str(target),
            "chars": len(report),
        },
        ensure_ascii=False,
    )


def verify_goal_validation_report(run_name: str, required_markers: str) -> str:
    """Verify that one persisted report contains every requested marker.

    Args:
        run_name: Stable lowercase validation scenario name.
        required_markers: JSON array of non-empty marker strings.
    """

    if not _SAFE_NAME.fullmatch(run_name):
        raise ValueError("run_name must be a safe lowercase identifier")
    markers = json.loads(required_markers)
    if not isinstance(markers, list) or not markers or not all(
        isinstance(marker, str) and marker.strip() for marker in markers
    ):
        raise ValueError("required_markers must be a non-empty JSON string array")
    target = _OUTPUT_ROOT / f"{run_name}.md"
    content = target.read_text(encoding="utf-8")
    missing = [marker for marker in markers if marker not in content]
    if missing:
        raise ValueError(f"validation report is missing markers: {missing}")
    return json.dumps(
        {
            "status": "verified",
            "path": str(target),
            "chars": len(content),
            "markers": markers,
        },
        ensure_ascii=False,
    )
