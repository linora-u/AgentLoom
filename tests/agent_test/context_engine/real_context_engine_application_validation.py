"""Run real LLM ContextEngine validation applications.

This script intentionally exercises full AgentLoom workflows instead of only
unit-level ContextEngine APIs:

* worker returns a large original payload
* supervisor sees a compressed preview plus ContextRef
* supervisor calls loom_retrieve_context
* local context store records entries and retrieval events
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.runner import run_app
from src.lib.config import C


@dataclass(frozen=True)
class ValidationCase:
    name: str
    workflow: str
    task: str
    expected_fragments: tuple[str, ...]
    min_entries: int
    expected_source_kinds: tuple[tuple[str, str], ...]


CASES: tuple[ValidationCase, ...] = (
    ValidationCase(
        name="text",
        workflow="applications/context_engine_text_retrieve_validation/workflows/context_engine_text_retrieve_validation_agent.yaml",
        task="Run the ContextEngine text retrieval validation. Follow the workflow exactly.",
        expected_fragments=("TEXT_CONTEXT_RETRIEVE_PASS", "TEXT-CTX-7319"),
        min_entries=1,
        expected_source_kinds=(("tool_result:make_context_engine_text_payload", "text"),),
    ),
    ValidationCase(
        name="json",
        workflow="applications/context_engine_json_retrieve_validation/workflows/context_engine_json_retrieve_validation_agent.yaml",
        task="Run the ContextEngine JSON retrieval validation. Follow the workflow exactly.",
        expected_fragments=("JSON_CONTEXT_RETRIEVE_PASS", "JSON-CTX-4927"),
        min_entries=1,
        expected_source_kinds=(("tool_result:make_context_engine_json_payload", "json"),),
    ),
    ValidationCase(
        name="multi",
        workflow="applications/context_engine_multi_worker_validation/workflows/context_engine_multi_worker_validation_agent.yaml",
        task="Run the ContextEngine multi-worker retrieval validation. Follow the workflow exactly.",
        expected_fragments=("MULTI_CONTEXT_RETRIEVE_PASS", "LOG-CTX-8842", "SEARCH-CTX-6194"),
        min_entries=2,
        expected_source_kinds=(
            ("tool_result:make_context_engine_log_payload", "log"),
            ("tool_result:make_context_engine_search_payload", "search"),
        ),
    ),
)


def _runtime_root() -> Path:
    return Path(os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "/tmp/agentloom-context-engine-apps")).resolve()


def _select_cases(selection: str) -> list[ValidationCase]:
    if selection == "all":
        return list(CASES)
    by_name = {case.name: case for case in CASES}
    try:
        return [by_name[selection]]
    except KeyError as exc:
        names = ", ".join(["all", *by_name])
        raise SystemExit(f"Unknown case {selection!r}. Expected one of: {names}") from exc


def _iter_context_entries(root: Path) -> Iterable[Path]:
    yield from root.glob("**/context_store/entries/*.json")


def _iter_context_events(root: Path) -> Iterable[Path]:
    yield from root.glob("**/context_store/events.jsonl")


def _load_entries(paths: Iterable[Path]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


def _count_retrieve_events(paths: Iterable[Path]) -> int:
    count = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "retrieved":
                count += 1
    return count


def _assert_no_legacy_large_result_path(root: Path, case_name: str) -> None:
    needles = ("Full output saved to", "agentloom_tool_results", "Output too large")
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        if path.suffix not in {".json", ".jsonl", ".log", ".txt", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for needle in needles:
            if needle in text:
                raise AssertionError(f"{case_name}: legacy large-result path leaked via {path}: {needle}")


def run_case(case: ValidationCase, *, runtime_root: Path) -> dict[str, object]:
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    previous_runtime_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT")
    os.environ["AGENT_LOOM_RUNTIME_ROOT"] = str(runtime_root)
    C.raw.setdefault("checkpoint", {})["cleanup_on_success"] = False
    C.raw.setdefault("lsp_servers", {})["enabled"] = False

    try:
        result = str(
            run_app(
                case.workflow,
                task_override=case.task,
                log_to_file=True,
            )
        )
    finally:
        if previous_runtime_root is None:
            os.environ.pop("AGENT_LOOM_RUNTIME_ROOT", None)
        else:
            os.environ["AGENT_LOOM_RUNTIME_ROOT"] = previous_runtime_root

    missing = [fragment for fragment in case.expected_fragments if fragment not in result]
    entry_paths = list(_iter_context_entries(runtime_root))
    event_paths = list(_iter_context_events(runtime_root))
    entries = _load_entries(entry_paths)
    refs = [entry["ref"] for entry in entries if isinstance(entry.get("ref"), str)]
    source_kinds = {(entry.get("source"), entry.get("kind")) for entry in entries}
    retrieve_events = _count_retrieve_events(event_paths)

    if missing:
        raise AssertionError(f"{case.name}: final result missing {missing}; result={result!r}")
    if len(refs) < case.min_entries:
        raise AssertionError(f"{case.name}: expected at least {case.min_entries} context entries, got {len(refs)}")
    missing_source_kinds = [item for item in case.expected_source_kinds if item not in source_kinds]
    if missing_source_kinds:
        got = sorted(f"{source}:{kind}" for source, kind in source_kinds)
        raise AssertionError(f"{case.name}: missing source/kind entries {missing_source_kinds}; got {got}")
    if retrieve_events < case.min_entries:
        raise AssertionError(f"{case.name}: expected at least {case.min_entries} retrieve events, got {retrieve_events}")
    _assert_no_legacy_large_result_path(runtime_root, case.name)

    return {
        "case": case.name,
        "status": "passed",
        "result": result,
        "context_refs": refs,
        "context_source_kinds": sorted(f"{source}:{kind}" for source, kind in source_kinds),
        "retrieve_events": retrieve_events,
        "runtime_root": str(runtime_root),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="all", help="Validation case: all, text, json, multi")
    args = parser.parse_args()

    root = _runtime_root()
    reports = []
    for case in _select_cases(args.case):
        reports.append(run_case(case, runtime_root=root / case.name))

    print(json.dumps({"status": "passed", "cases": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
