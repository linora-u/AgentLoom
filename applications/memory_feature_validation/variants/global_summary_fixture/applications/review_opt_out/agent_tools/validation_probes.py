"""Local case loader for the isolated review opt-out Application."""

from __future__ import annotations

import json
import os
from pathlib import Path

_MEMORY_CASE_ENV = "AGENTLOOM_MEMORY_CASE_ID"
_MEMORY_PHASE_ENV = "AGENTLOOM_MEMORY_CASE_PHASE"

# The fixture is an AgentLoom root in its own right, so its runtime cannot
# import tools through the outer repository's ``applications`` namespace.
# Data remains canonical in the enclosing validation Application.
_VALIDATION_ROOT = Path(__file__).resolve().parents[5]
_CASES_PATH = _VALIDATION_ROOT / "data" / "cases.jsonl"
_FIXTURE_ROOT = (_VALIDATION_ROOT / "data" / "fixtures").resolve()


def _jsonl_row(path: Path, case_id: str) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("case_id") == case_id:
            return row
    raise KeyError(f"unknown memory validation case: {case_id}")


def validation_memory_case() -> str:
    """Return one model-visible case without exposing the hidden oracle."""

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
            raise RuntimeError(
                "validation fixture escaped the fixture root"
            ) from exc
        fixture = _jsonl_row(fixture_path, case_id)
        payload = {
            "case_id": case_id,
            "phase": phase,
            "task": str(case.get("writer_task") or ""),
            "evidence": fixture.get("evidence"),
            "memory_evidence": fixture.get("memory_evidence", []),
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
