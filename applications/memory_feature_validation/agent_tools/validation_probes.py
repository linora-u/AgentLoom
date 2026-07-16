"""Model-visible case loader for the memory review validation app."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.lib.smolagents.tools import trusted_memory_evidence

_MEMORY_CASE_ENV = "AGENTLOOM_MEMORY_CASE_ID"
_MEMORY_PHASE_ENV = "AGENTLOOM_MEMORY_CASE_PHASE"

_APP_ROOT = Path(__file__).resolve().parents[1]
_CASES_PATH = _APP_ROOT / "data" / "cases.jsonl"
_FIXTURE_ROOT = (_APP_ROOT / "data" / "fixtures").resolve()


def _jsonl_row(path: Path, case_id: str) -> dict:
    """Load one validation row without consulting the hidden oracle."""

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("case_id") == case_id:
            return row
    raise KeyError(f"unknown memory validation case: {case_id}")


def extract_validation_memory_evidence(result_json: str) -> list[dict[str, str]]:
    """Extract explicitly trusted durable facts from a validation tool result.

    This stays pure so ``validation_memory_case`` can later be annotated by the
    framework's trusted-memory-evidence decorator without coupling the fixture
    loader to a framework module that does not exist yet. Malformed annotations
    fail closed as an empty evidence set.
    """

    try:
        payload = json.loads(result_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    raw_evidence = payload.get("memory_evidence")
    if not isinstance(raw_evidence, list):
        return []

    extracted: list[dict[str, str]] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            return []
        kind = item.get("kind")
        scope = item.get("scope")
        source = item.get("source")
        text = item.get("text")
        if (
            kind != "durable_fact"
            or scope not in {"project", "application"}
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(text, str)
            or not text.strip()
        ):
            return []
        extracted.append(
            {"kind": kind, "scope": scope, "source": source, "text": text}
        )
    return extracted


def validation_memory_case() -> str:
    """Return a natural task and, for writer phases, its fixture evidence.

    Recall phases expose only the follow-up question. Expected status, scope,
    recall value, and security markers live in ``oracle/cases.jsonl`` and are
    never available through this tool.
    """

    case_id = os.environ.get(_MEMORY_CASE_ENV, "").strip()
    phase = os.environ.get(_MEMORY_PHASE_ENV, "writer").strip() or "writer"
    if not case_id:
        raise RuntimeError(f"{_MEMORY_CASE_ENV} is required")

    case = _jsonl_row(_CASES_PATH, case_id)
    if phase == "writer":
        fixture_name = str(case.get("fixture") or "").strip()
        fixture_path = (_FIXTURE_ROOT / fixture_name).resolve()
        try:
            fixture_path.relative_to(_FIXTURE_ROOT)
        except ValueError as exc:
            raise RuntimeError("validation fixture escaped the fixture root") from exc
        fixture = _jsonl_row(fixture_path, case_id)
        evidence_scope = (
            "application"
            if str(case.get("scenario") or "") == "application_scope"
            else "project"
        )
        raw_memory_evidence = fixture.get("memory_evidence", [])
        memory_evidence = [
            {**item, "scope": evidence_scope}
            for item in raw_memory_evidence
            if isinstance(item, dict)
        ]
        payload = {
            "case_id": case_id,
            "phase": phase,
            "task": str(case.get("writer_task") or ""),
            "evidence": fixture.get("evidence"),
            "memory_evidence": memory_evidence,
        }
    else:
        payload = {
            "case_id": case_id,
            "phase": phase,
            "task": str(case.get("recall_task") or ""),
            "evidence": None,
            "memory_evidence": [],
            "constraint": (
                "Do not reopen or reconstruct the earlier evidence. Use only "
                "the persistent memory snapshot supplied at run start."
            ),
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# Bind metadata after the definition so AgentLoom's dynamic Tool source keeps
# only the model-visible function body; the extractor remains code-owned.
validation_memory_case = trusted_memory_evidence(
    extract_validation_memory_evidence
)(validation_memory_case)
