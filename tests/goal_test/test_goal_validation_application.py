import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.lib.goal import GoalState, GoalStateProvider, bind_goal_state_provider

_REPORT_TOOLS_PATH = (
    Path(__file__).parents[2]
    / "applications"
    / "goal_mode_validation"
    / "agent_tools"
    / "report_tools.py"
)
_SPEC = importlib.util.spec_from_file_location("goal_validation_report_tools", _REPORT_TOOLS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
report_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_tools)


def _provider() -> GoalStateProvider:
    return GoalStateProvider(
        GoalState.create(
            objective="Validate Goal mode.",
            objective_fingerprint="validation-app",
            token_budget=50_000,
        )
    )


@pytest.mark.parametrize(
    "topic", ["contract", "lifecycle", "observability", "documentation"]
)
def test_goal_validation_evidence_is_bounded_and_traceable(topic):
    payload = json.loads(report_tools.load_goal_audit_evidence(topic))

    assert payload["topic"] == topic
    assert payload["files"]
    for record in payload["files"]:
        assert record["path"]
        assert len(record["sha256"]) == 64
        assert record["excerpts"]
        assert len(record["excerpts"]) <= report_tools._EVIDENCE_LINES_PER_FILE


def test_goal_validation_evidence_rejects_unknown_topic():
    with pytest.raises(ValueError, match="topic must be"):
        report_tools.load_goal_audit_evidence("unknown")


def test_parallel_budget_probe_reuses_report_for_same_goal(tmp_path, monkeypatch):
    provider = _provider()
    goal_id = provider.snapshot().goal_id
    monkeypatch.setattr(report_tools, "_OUTPUT_ROOT", tmp_path)
    (tmp_path / "parallel_budget.md").write_text(
        "\n".join(
            [
                "# Parallel Goal Budget",
                "## Batch Results",
                "## Accounting",
                f"goal_id={goal_id}",
                "## Resume Instructions",
            ]
        ),
        encoding="utf-8",
    )

    def fail_if_rerun(*_args, **_kwargs):
        raise AssertionError("resume must not rerun the completed Worker batch")

    monkeypatch.setattr(report_tools, "run_goal_audit_batch", fail_if_rerun)
    with bind_goal_state_provider(provider):
        result = json.loads(
            report_tools.run_parallel_goal_budget_probe(
                '[{"task_id":"audit-1","query":"Inspect the Goal contract."}]'
            )
        )

    assert result["status"] == "existing_report"
    assert result["batch_rerun"] is False
    assert result["report"]["matches_current_goal"] is True


def test_goal_audit_batch_injects_evidence_into_tool_free_worker_query(monkeypatch):
    from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

    captured = {}

    def fake_parallel(_worker_path, tasks, *, max_workers):
        captured["tasks"] = tasks
        captured["max_workers"] = max_workers
        return [
            SimpleNamespace(
                task_id="audit-1",
                status="completed",
                duration_seconds=0.1,
                result="VERDICT: PASS",
                error=None,
            )
        ]

    monkeypatch.setattr(YamlAgentFactory, "run_agents_parallel", fake_parallel)
    result = json.loads(
        report_tools.run_goal_audit_batch(
            "contract",
            '[{"task_id":"audit-1","query":"Inspect the strict Goal contract."}]',
        )
    )

    assert result[0]["status"] == "completed"
    assert captured["max_workers"] == 1
    query = captured["tasks"][0]["query"]
    assert "EVIDENCE_BUNDLE=" in query
    assert '"src/lib/goal/model.py"' in query
