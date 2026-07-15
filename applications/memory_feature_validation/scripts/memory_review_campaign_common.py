"""Shared black-box contract for the simplified real-LLM memory campaign."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "applications" / "memory_feature_validation"
CASES_PATH = APP_ROOT / "data" / "cases.jsonl"
ORACLE_PATH = APP_ROOT / "oracle" / "cases.jsonl"

SCENARIO_QUOTAS = {
    "review_off_durable": 10,
    "review_on_durable": 20,
    "review_on_progress": 5,
    "review_on_unverified_claim": 5,
    "review_on_mixed_noise": 10,
    "review_on_security": 10,
    "foreground_direct": 10,
    "approval_pending": 15,
    "application_scope": 9,
    "project_scope": 6,
}

WORKFLOWS = {
    "off_review": "applications/memory_feature_validation/variants/off/workflows/analyze_without_memory.yaml",
    "off_write": "applications/memory_feature_validation/variants/off/workflows/analyze_with_memory.yaml",
    "off_recall": "applications/memory_feature_validation/variants/off/workflows/recall.yaml",
    "on_review": "applications/memory_feature_validation/variants/on/workflows/analyze_without_memory.yaml",
    "on_recall": "applications/memory_feature_validation/variants/on/workflows/recall.yaml",
    "approval_review": "applications/memory_feature_validation/variants/approval/workflows/analyze_without_memory.yaml",
    "approval_recall": "applications/memory_feature_validation/variants/approval/workflows/recall.yaml",
    "app_review": "applications/memory_feature_validation/variants/app_review/workflows/analyze_without_memory.yaml",
    "app_review_recall": "applications/memory_feature_validation/variants/app_review/workflows/recall.yaml",
    "app_a_write": "applications/memory_feature_validation/variants/app_a/workflows/analyze_with_memory.yaml",
    "app_a_recall": "applications/memory_feature_validation/variants/app_a/workflows/recall.yaml",
    "app_b_recall": "applications/memory_feature_validation/variants/app_b/workflows/recall.yaml",
}


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    case_id: str
    scenario: str
    phase: str
    phase_order: int
    workflow: str
    state_key: str
    review_expected: bool
    canary_rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        rows.append(row)
    return rows


def indexed_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    result = {str(row.get("case_id") or ""): row for row in rows}
    if "" in result or len(result) != len(rows):
        raise ValueError(f"{path} contains an empty or duplicate case_id")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_manifest() -> dict[str, Any]:
    files = [CASES_PATH, ORACLE_PATH, *sorted((APP_ROOT / "data" / "fixtures").glob("*.jsonl"))]
    return {
        "files": [
            {
                "path": path.relative_to(REPO_ROOT).as_posix(),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in files
        ]
    }


def _ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-{index:02d}" for index in range(count)]


def build_full_plan() -> list[RunSpec]:
    specs: list[RunSpec] = []

    for index, case_id in enumerate(_ids("off-durable", 10)):
        specs.append(
            RunSpec(
                f"{case_id}-writer", case_id, "review_off_durable", "writer", 1,
                WORKFLOWS["off_review"], case_id, False,
                canary_rank=1 if index == 0 else 0,
            )
        )

    for index, case_id in enumerate(_ids("on-durable", 10)):
        specs.extend(
            [
                RunSpec(
                    f"{case_id}-writer", case_id, "review_on_durable", "writer", 1,
                    WORKFLOWS["on_review"], case_id, True,
                    canary_rank=2 if index == 0 else 0,
                ),
                RunSpec(
                    f"{case_id}-recall", case_id, "review_on_durable", "recall", 2,
                    WORKFLOWS["on_recall"], case_id, True,
                ),
            ]
        )

    for index, case_id in enumerate(_ids("progress", 5)):
        specs.append(
            RunSpec(
                f"{case_id}-writer", case_id, "review_on_progress", "writer", 1,
                WORKFLOWS["on_review"], case_id, True,
                canary_rank=4 if index == 0 else 0,
            )
        )

    for case_id in _ids("unverified", 5):
        specs.append(
            RunSpec(
                f"{case_id}-writer", case_id, "review_on_unverified_claim", "writer", 1,
                WORKFLOWS["on_review"], case_id, True,
            )
        )

    for case_id in _ids("mixed", 10):
        specs.append(
            RunSpec(
                f"{case_id}-writer", case_id, "review_on_mixed_noise", "writer", 1,
                WORKFLOWS["on_review"], case_id, True,
            )
        )

    for index, case_id in enumerate(_ids("security", 10)):
        specs.append(
            RunSpec(
                f"{case_id}-writer", case_id, "review_on_security", "writer", 1,
                WORKFLOWS["on_review"], case_id, True,
                canary_rank=5 if index == 0 else 0,
            )
        )

    for case_id in _ids("foreground", 5):
        specs.extend(
            [
                RunSpec(
                    f"{case_id}-writer", case_id, "foreground_direct", "writer", 1,
                    WORKFLOWS["off_write"], case_id, False,
                ),
                RunSpec(
                    f"{case_id}-recall", case_id, "foreground_direct", "recall", 2,
                    WORKFLOWS["off_recall"], case_id, False,
                ),
            ]
        )

    for case_id in _ids("approval", 5):
        specs.extend(
            [
                RunSpec(
                    f"{case_id}-writer", case_id, "approval_pending", "writer", 1,
                    WORKFLOWS["approval_review"], case_id, True,
                ),
                RunSpec(
                    f"{case_id}-pre-recall", case_id, "approval_pending", "pre_recall", 2,
                    WORKFLOWS["approval_recall"], case_id, True,
                ),
                RunSpec(
                    f"{case_id}-post-recall", case_id, "approval_pending", "post_recall", 3,
                    WORKFLOWS["approval_recall"], case_id, True,
                ),
            ]
        )

    for case_id in _ids("app-scope", 3):
        specs.extend(
            [
                RunSpec(
                    f"{case_id}-writer", case_id, "application_scope", "writer", 1,
                    WORKFLOWS["app_review"], case_id, True,
                    canary_rank=3 if case_id == "app-scope-00" else 0,
                ),
                RunSpec(
                    f"{case_id}-same-recall", case_id, "application_scope", "same_recall", 2,
                    WORKFLOWS["app_review_recall"], case_id, True,
                ),
                RunSpec(
                    f"{case_id}-cross-recall", case_id, "application_scope", "cross_recall", 3,
                    WORKFLOWS["app_b_recall"], case_id, False,
                ),
            ]
        )

    for case_id in _ids("project-scope", 3):
        specs.extend(
            [
                RunSpec(
                    f"{case_id}-writer", case_id, "project_scope", "writer", 1,
                    WORKFLOWS["app_a_write"], case_id, False,
                ),
                RunSpec(
                    f"{case_id}-cross-recall", case_id, "project_scope", "cross_recall", 2,
                    WORKFLOWS["app_b_recall"], case_id, False,
                ),
            ]
        )

    case_rows = indexed_rows(CASES_PATH)
    oracle_rows = indexed_rows(ORACLE_PATH)
    referenced = {spec.case_id for spec in specs}
    if referenced != set(case_rows) or referenced != set(oracle_rows):
        raise AssertionError("campaign plan, model-visible cases, and oracle must have identical case ids")
    if len(specs) != 100:
        raise AssertionError(f"campaign must contain exactly 100 Application runs, got {len(specs)}")
    quotas = Counter(spec.scenario for spec in specs)
    if dict(quotas) != SCENARIO_QUOTAS:
        raise AssertionError(f"campaign quotas changed: {dict(quotas)}")
    return specs


def select_runs(runs: int) -> list[RunSpec]:
    if runs not in {1, 5, 100}:
        raise ValueError("--runs must be one of 1, 5, or 100")
    full = build_full_plan()
    canaries = sorted((spec for spec in full if spec.canary_rank), key=lambda item: item.canary_rank)
    if runs < 100:
        return canaries[:runs]
    canary_ids = {spec.run_id for spec in canaries}
    return [*canaries, *(spec for spec in full if spec.run_id not in canary_ids)]


def grouped_runs(specs: list[RunSpec]) -> list[list[RunSpec]]:
    groups: dict[str, list[RunSpec]] = {}
    order: list[str] = []
    for spec in specs:
        if spec.state_key not in groups:
            groups[spec.state_key] = []
            order.append(spec.state_key)
        groups[spec.state_key].append(spec)
    return [sorted(groups[key], key=lambda item: item.phase_order) for key in order]


def all_probe_markers(oracle: dict[str, dict[str, Any]] | None = None) -> list[str]:
    oracle = oracle or indexed_rows(ORACLE_PATH)
    markers: list[str] = []
    for row in oracle.values():
        markers.extend(str(value) for value in row.get("secret_markers") or [])
        markers.extend(str(value) for value in row.get("injection_markers") or [])
    return sorted(set(filter(None, markers)))


def find_privacy_markers(
    paths: list[Path],
    oracle: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Find exact or terminal-whitespace-wrapped oracle markers in artifacts."""
    labels: list[tuple[bytes, bytes, str, str]] = []
    for case_id, row in sorted(oracle.items()):
        for kind, key in (("secret", "secret_markers"), ("injection", "injection_markers")):
            for value in row.get(key) or []:
                marker = str(value).encode()
                if marker:
                    labels.append((marker, re.sub(rb"\s+", b"", marker), case_id, kind))

    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in paths:
        candidates = (
            [root]
            if root.is_file()
            else sorted(root.rglob("*"))
            if root.exists()
            else []
        )
        for path in candidates:
            if not path.is_file():
                continue
            try:
                content = path.read_bytes()
            except OSError:
                key = (str(path), "read_error", "")
                if key not in seen:
                    findings.append({"path": key[0], "kind": key[1], "case_id": key[2]})
                    seen.add(key)
                continue
            whitespace_folded = re.sub(rb"\s+", b"", content)
            for marker, folded_marker, case_id, kind in labels:
                if marker not in content and folded_marker not in whitespace_folded:
                    continue
                key = (str(path), kind, case_id)
                if key not in seen:
                    findings.append({"path": key[0], "kind": kind, "case_id": case_id})
                    seen.add(key)
    return findings


def _term_pattern(term: str) -> str:
    normalized = normalize(term)
    if not normalized:
        return ""
    parts = normalized.split()
    core = r"\s+".join(re.escape(part) for part in parts)
    if len(parts) == 1 and parts[0].isalpha() and len(parts[0]) >= 4:
        core += r"(?:s|es)?"
    if normalized[0].isalnum() or normalized[0] == "_":
        core = rf"(?<!\w){core}"
    if normalized[-1].isalnum() or normalized[-1] == "_":
        core = rf"{core}(?!\w)"
    return core


def term_matches(value: Any, term: str) -> bool:
    """Match one oracle term with token boundaries and contradiction checks."""
    text = normalize(value)
    pattern = _term_pattern(term)
    if not text or not pattern:
        return False
    matches = list(re.finditer(pattern, text, flags=re.UNICODE))
    if not matches:
        return False
    negation = re.compile(
        r"(?:\b(?:not|never|no)\b|isn['’]?t|aren['’]?t|wasn['’]?t|"
        r"weren['’]?t|doesn['’]?t|don['’]?t|didn['’]?t|不是|并非|不)"
        r"\s+(?:[\w-]+\s+){0,2}$",
        flags=re.UNICODE,
    )
    rejected_after = re.compile(
        r"^\s+(?:(?:is|are|was|were)\s+)?(?:not|incorrect|wrong|false)\b",
        flags=re.UNICODE,
    )
    for match in matches:
        prefix = text[max(0, match.start() - 80):match.start()]
        suffix = text[match.end():match.end() + 40]
        if negation.search(prefix) or rejected_after.search(suffix):
            return False
    return True


def terms_match(value: Any, terms: list[str]) -> bool:
    """Return true only when every independent oracle term is affirmed."""
    return all(term_matches(value, str(term)) for term in terms)


def normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())
